"""`GET /companies`: the companies the tools have data for, so the research
view can offer them in a dropdown. Read from the same snapshot the tools
query; it never calls a model.
"""

from functools import cache

from fastapi import APIRouter

from recon.api.schemas import Company, CompanyList
from recon.tools.data_source import open_tool_data
from recon.tools.server import list_companies

router = APIRouter()


@cache
def _companies() -> CompanyList:
    """Loaded once per process: the snapshot doesn't change under a running
    server, and loading it reads the Parquet files."""
    rows = list_companies(open_tool_data()).data
    return CompanyList(
        companies=sorted(
            (Company(company_id=row["company_id"], name=row["name"]) for row in rows),
            key=lambda company: company.company_id,
        )
    )


@router.get("/companies")
async def companies() -> CompanyList:
    """Every company the tools know, by ticker."""
    return _companies()
