"""Tests for `api/health.py` dependency checks. `docs/contracts.md` section 5.

`check_postgres` is exercised directly here (not just via the monkeypatched
version in `test_api.py`) to cover its actual error handling.
"""

import pytest

from recon.api.health import check_postgres

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
