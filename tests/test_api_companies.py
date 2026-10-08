"""`GET /companies`: the companies the tools have data for, for the research
view's company dropdown. Read from the same snapshot the tools query
(`tests/conftest.py` pins the synthetic fixtures)."""

import pytest
from httpx import ASGITransport, AsyncClient

from recon.api import main as api_main
from recon.tools.data_source import open_tool_data
from recon.tools.server import list_companies

pytestmark = [pytest.mark.unit, pytest.mark.anyio]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_companies_lists_every_company_the_tools_know_sorted_by_ticker() -> None:
    expected = sorted(
        row["company_id"] for row in list_companies(open_tool_data()).data
    )
    transport = ASGITransport(app=api_main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/companies")

    assert resp.status_code == 200
    companies = resp.json()["companies"]
    assert [c["company_id"] for c in companies] == expected
    assert all(c["name"] for c in companies)
