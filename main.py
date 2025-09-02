"""
FDA Drug Tools MCP server optimized for Google Cloud Run deployment.
Exposes 7 tools for querying FDA drug label data.
Transport: HTTP (Claude.ai compatible)
Endpoint: /mcp
"""

import os
import httpx
import logging
import re
from typing import List, Optional
from pydantic import BaseModel, Field
from fastmcp import FastMCP

# Configure logging for Cloud Run
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s'
)
log = logging.getLogger("openfda_mcp")

# FastMCP setup
mcp = FastMCP(
    "FDA Drug Tools",
    instructions="Query FDA drug-label data in real time"
)

# Configuration
OPENFDA_URL = "https://api.fda.gov/drug/label.json"
TIMEOUT = 30
MAX_RETRIES = 3

class DrugInfo(BaseModel):
    brand_names: List[str] = Field(..., alias="brandNames")
    generic_names: List[str] = Field(..., alias="genericNames")
    manufacturer: List[str]
    indications: List[str]
    ndc_codes: List[str] = Field(..., alias="ndcCodes")

async def _fetch_openfda_with_retry(params: dict) -> dict:
    """Fetch data from OpenFDA API with retry logic for Cloud Run reliability"""
    for attempt in range(MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                log.info(f"FDA API query (attempt {attempt + 1}): {params}")
                r = await client.get(OPENFDA_URL, params=params)
                
                if r.status_code == 404:
                    log.info(f"No results found for query: {params.get('search', 'N/A')}")
                    return {"results": []}
                
                r.raise_for_status()
                result = r.json()
                log.info(f"Found {len(result.get('results', []))} results")
                return result
                
        except httpx.TimeoutException as e:
            log.warning(f"Timeout on attempt {attempt + 1}: {e}")
            if attempt == MAX_RETRIES - 1:
                raise
        except httpx.HTTPError as e:
            log.error(f"HTTP error on attempt {attempt + 1}: {e}")
            if attempt == MAX_RETRIES - 1:
                raise
        except Exception as e:
            log.error(f"Unexpected error on attempt {attempt + 1}: {e}")
            if attempt == MAX_RETRIES - 1:
                raise

def _normalize_ndc(ndc_input: str) -> List[str]:
    """Normalize NDC formats for better search results"""
    if not ndc_input:
        return []
    
    ndc_input = ndc_input.strip()
    formats = [ndc_input]
    
    # Handle hyphenated NDCs
    if '-' in ndc_input:
        clean_ndc = re.sub(r'[^\d]', '', ndc_input)
        if len(clean_ndc) >= 9:
            formats.append(clean_ndc)
    else:
        # Handle non-hyphenated NDCs
        clean_ndc = re.sub(r'[^\d]', '', ndc_input)
        if len(clean_ndc) == 10:
            formats.append(f"{clean_ndc[:5]}-{clean_ndc[5:9]}-{clean_ndc[9:]}")
        elif len(clean_ndc) == 11:
            formats.append(f"{clean_ndc[:5]}-{clean_ndc[5:9]}-{clean_ndc[9:]}")
    
    return list(dict.fromkeys(formats))[:3]

def _build_search(
    drug: Optional[str],
    manufacturer: Optional[str] = None,
    dosage_form: Optional[str] = None,
    route: Optional[str] = None,
    ndc: Optional[str] = None,
    exact: bool = False
) -> str:
    """Build OpenFDA search query"""
    
    # Prioritize NDC searches
    if ndc:
        ndc_formats = _normalize_ndc(ndc)
        if ndc_formats:
            ndc_queries = [f'openfda.product_ndc:"{ndc_format}"' for ndc_format in ndc_formats]
            ndc_query = "(" + " OR ".join(ndc_queries) + ")"
            
            additional_filters = []
            if manufacturer:
                additional_filters.append(f'openfda.manufacturer_name:"{manufacturer}"')
            if dosage_form:
                additional_filters.append(f'openfda.dosage_form:"{dosage_form}"')
            if route:
                additional_filters.append(f'openfda.route:"{route}"')
            
            if not additional_filters and not drug:
                return ndc_query
            
            query_parts = [ndc_query]
            if drug:
                fields = ["openfda.brand_name", "openfda.generic_name", "openfda.substance_name"]
                drug_query = "(" + " OR ".join(
                    f'{field}.exact:"{drug}"' if exact else f'{field}:"{drug}"'
                    for field in fields
                ) + ")"
                query_parts.append(drug_query)
            
            query_parts.extend(additional_filters)
            return " AND ".join(query_parts)
    
    # Non-NDC searches
    query_parts = []
    
    if drug:
        fields = ["openfda.brand_name", "openfda.generic_name", "openfda.substance_name"]
        drug_query = "(" + " OR ".join(
            f'{field}.exact:"{drug}"' if exact else f'{field}:"{drug}"'
            for field in fields
        ) + ")"
        query_parts.append(drug_query)

    if manufacturer:
        query_parts.append(f'openfda.manufacturer_name:"{manufacturer}"')
    if dosage_form:
        query_parts.append(f'openfda.dosage_form:"{dosage_form}"')
    if route:
        query_parts.append(f'openfda.route:"{route}"')

    return " AND ".join(query_parts) if query_parts else "*:*"

@mcp.tool(
    name="get_drug_indications",
    description="Returns FDA-approved indications. Supports filtering by drug name, NDC, manufacturer, dosage form, and route."
)
async def get_drug_indications(
    drug_name: Optional[str] = None,
    manufacturer: Optional[str] = None,
    dosage_form: Optional[str] = None,
    route: Optional[str] = None,
    ndc: Optional[str] = None,
    limit: int = 3,
    exact_match: bool = False
) -> List[DrugInfo]:
    params = {
        "search": _build_search(drug_name, manufacturer, dosage_form, route, ndc, exact_match),
        "limit": max(1, min(limit, 10))
    }
    
    data = await _fetch_openfda_with_retry(params)
    if not data.get("results"):
        return []
    
    out = []
    for rec in data["results"]:
        ofda = rec.get("openfda", {})
        out.append(DrugInfo(
            brandNames=ofda.get("brand_name", []),
            genericNames=ofda.get("generic_name", []),
            manufacturer=ofda.get("manufacturer_name", []),
            indications=rec.get("indications_and_usage", []),
            ndcCodes=ofda.get("product_ndc", []),
        ))
    return out

def _create_simple_tool(section: str, tool_name: str, description: str):
    """Factory function to create simple tools"""
    @mcp.tool(name=tool_name, description=description)
    async def tool(
        drug_name: Optional[str] = None,
        manufacturer: Optional[str] = None,
        dosage_form: Optional[str] = None,
        route: Optional[str] = None,
        ndc: Optional[str] = None,
        limit: int = 3,
        exact_match: bool = False
    ) -> List[str]:
        params = {
            "search": _build_search(drug_name, manufacturer, dosage_form, route, ndc, exact_match),
            "limit": max(1, min(limit, 10))
        }
        
        data = await _fetch_openfda_with_retry(params)
        if not data.get("results"):
            return []
        
        out = []
        for rec in data["results"]:
            section_data = rec.get(section, [])
            out.extend(section_data)
        return out
    return tool

# Create all tools
get_drug_dosage = _create_simple_tool(
    "dosage_and_administration",
    "get_drug_dosage",
    "Returns FDA-approved dosage and administration instructions. Supports filtering by drug name, NDC, manufacturer, dosage form, and route."
)

get_specific_populations = _create_simple_tool(
    "use_in_specific_populations",
    "get_specific_populations",
    "Returns FDA 'Use in Specific Populations' info. Supports filtering by drug name, NDC, manufacturer, dosage form, and route."
)

get_storage_handling = _create_simple_tool(
    "how_supplied_storage_and_handling",
    "get_storage_handling",
    "Returns FDA 'How Supplied/Storage and Handling' info. Supports filtering by drug name, NDC, manufacturer, dosage form, and route."
)

get_warnings_precautions = _create_simple_tool(
    "warnings_and_precautions",
    "get_warnings_precautions",
    "Returns FDA 'Warnings and Precautions' info. Supports filtering by drug name, NDC, manufacturer, dosage form, and route."
)

get_clinical_pharmacology = _create_simple_tool(
    "clinical_pharmacology",
    "get_clinical_pharmacology",
    "Returns FDA 'Clinical Pharmacology' info. Supports filtering by drug name, NDC, manufacturer, dosage form, and route."
)

get_drug_description = _create_simple_tool(
    "description",
    "get_drug_description",
    "Returns FDA-approved product description. Supports filtering by drug name, NDC, manufacturer, dosage form, and route."
)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    log.info(f"Starting FDA Drug Tools MCP server on port {port}")
    
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=port,
        path="/mcp",
        log_level="info"
    )
