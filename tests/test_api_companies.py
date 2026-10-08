"""`GET /companies`: the companies the tools have data for, for the research
view's company dropdown. Read from the same snapshot the tools query
(`tests/conftest.py` pins the synthetic fixtures)."""

import pytest
from httpx import ASGITransport, AsyncClient

from recon.api import companies as companies_module
from recon.api import main as api_main
from recon.contracts import ToolResult
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


async def test_companies_does_not_cache_a_transient_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DuckDB hiccup on the first call must not be baked in forever: once
    the source recovers, the next request should see real data again."""
    companies_module._companies.cache_clear()
    calls = {"n": 0}

    def flaky(conn: object) -> ToolResult:
        calls["n"] += 1
        if calls["n"] == 1:
            return ToolResult(
                status="unavailable", data=[], row_count=0, message="boom", elapsed_ms=0
            )
        return list_companies(open_tool_data())

    monkeypatch.setattr(companies_module, "list_companies", flaky)
    transport = ASGITransport(app=api_main.app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.get("/companies")
            second = await client.get("/companies")
    finally:
        companies_module._companies.cache_clear()

    assert first.status_code == 503
    assert second.status_code == 200
    assert second.json()["companies"]


async def test_companies_tolerates_a_company_with_no_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The EDGAR submission a ticker is built from doesn't always carry a
    name (`_company_row` in sec_edgar_normalize.py); that ticker must still
    show up in the dropdown instead of a 500."""
    companies_module._companies.cache_clear()

    def no_name(conn: object) -> ToolResult:
        return ToolResult(
            status="ok",
            data=[{"company_id": "NONAME", "name": None}],
            row_count=1,
            message="ok",
            elapsed_ms=0,
        )

    monkeypatch.setattr(companies_module, "list_companies", no_name)
    transport = ASGITransport(app=api_main.app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/companies")
    finally:
        companies_module._companies.cache_clear()

    assert resp.status_code == 200
    assert resp.json()["companies"] == [{"company_id": "NONAME", "name": None}]
