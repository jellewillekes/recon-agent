"""`GET /companies`: the companies the tools have data for, so the research
view can offer them in a dropdown. Read from the same snapshot the tools
query; it never calls a model.
"""

from functools import cache

from fastapi import APIRouter, HTTPException

from recon.api.schemas import Company, CompanyList
from recon.tools.data_source import open_tool_data
from recon.tools.server import list_companies

router = APIRouter()


@cache
def _companies() -> CompanyList:
    """Loaded once per process: the snapshot doesn't change under a running
    server, and loading it reads the Parquet files. Raising instead of
    returning on `unavailable` keeps a transient failure from being cached
    forever — `functools.cache` only stores a call that returns normally,
    so the retry/circuit-breaker recovery in `execution.py` still applies
    on the next request."""
    result = list_companies(open_tool_data())
    if result.status == "unavailable":
        raise HTTPException(status_code=503, detail=result.message)
    return CompanyList(
        companies=sorted(
            (
                Company(company_id=row["company_id"], name=row["name"])
                for row in result.data
            ),
            key=lambda company: company.company_id,
        )
    )


@router.get("/companies")
async def companies() -> CompanyList:
    """Every company the tools know, by ticker."""
    return _companies()
