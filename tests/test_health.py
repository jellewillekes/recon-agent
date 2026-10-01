"""Tests for `api/health.py` dependency checks. `docs/contracts.md` section 5.

`check_postgres` is exercised directly here (not just via the monkeypatched
version in `test_api.py`) to cover its actual error handling.
"""

import pytest

from recon.api.health import check_mcp_server, check_postgres

pytestmark = [pytest.mark.unit, pytest.mark.anyio]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_check_postgres_no_database_url_configured() -> None:
    ok, detail = await check_postgres(None, 5.0)

    assert ok is False
    assert detail == "DATABASE_URL not configured"


async def test_check_postgres_malformed_dsn_is_reported_not_raised() -> None:
    """`asyncpg.connect` raises `ValueError` for a malformed DSN, before it
    ever opens a connection — a typo'd `DATABASE_URL` must surface as a
    reported failure, not propagate out of the check.
    """
    ok, detail = await check_postgres("not-a-valid-dsn", 5.0)

    assert ok is False
    assert "Postgres unreachable" in detail


async def test_check_mcp_server_passes_the_environment_to_the_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The server must see `RECON_TOOL_DATA`. Containers set it to `fixture`
    because they carry no EDGAR cache; dropped, the server refuses to start.
    A bogus value failing proves the variable arrives even on a machine whose
    EDGAR cache would make the default work.
    """
    monkeypatch.setenv("RECON_TOOL_DATA", "fixture")
    assert await check_mcp_server(30.0) == (True, "ok")

    monkeypatch.setenv("RECON_TOOL_DATA", "not-a-source")
    ok, detail = await check_mcp_server(30.0)
    assert ok is False
    assert "MCP server unreachable" in detail
