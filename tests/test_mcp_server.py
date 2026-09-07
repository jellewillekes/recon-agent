"""Tests for `tools/mcp_server.py`'s env-var-reading wrapper around the
write path. The other four `@server.tool()` closures aren't exercised here
either (per the module's own docstring, `tests/test_tools.py` calls the
plain `server.py` functions directly) — this file covers only the piece
`mcp_server.py` itself adds: reading `RECON_CREATED_BY`/`DATABASE_URL` from
the environment, per PR #43's round-4 review.
"""

import pytest

from recon.tools.mcp_server import _call_flag_case_for_review

pytestmark = [pytest.mark.unit, pytest.mark.anyio]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_dry_run_reflects_created_by_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RECON_CREATED_BY", "agent_sdk:multi")

    result = await _call_flag_case_for_review(
        "case-001", "reason", "key-001", True, False, None
    )

    assert result["status"] == "would_write"
    assert result["flag"]["created_by"] == "agent_sdk:multi"


async def test_created_by_falls_back_when_env_var_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RECON_CREATED_BY", raising=False)

    result = await _call_flag_case_for_review(
        "case-001", "reason", "key-001", True, False, None
    )

    assert result["flag"]["created_by"] == "agent_sdk:unknown"


async def test_confirmed_call_without_database_url_env_var_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    preview = await _call_flag_case_for_review(
        "case-001", "reason", "key-001", False, False, None
    )

    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        await _call_flag_case_for_review(
            "case-001", "reason", "key-001", False, True, preview["preview_token"]
        )
