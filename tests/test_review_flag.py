"""Tests for the write path, `tools/review_flag.py` (`docs/contracts.md` section 6).

`dry_run`/unconfirmed calls never touch Postgres at all — proven here by simply
not patching `asyncpg.connect` for those tests: if the code under test tried to
connect anyway, it would fail against a nonexistent database and the test would
fail loudly, not silently pass.

The confirmed-write tests patch `asyncpg.connect` with a fake connection backed
by an in-memory dict, faithfully reproducing the real SQL's
`INSERT ... ON CONFLICT (idempotency_key) DO NOTHING RETURNING *` / fallback
`SELECT` idempotency semantics — same monkeypatching philosophy as
`tests/test_multi_agent.py`'s fake `query()` stream.
"""

from typing import Any

import pytest

from recon.contracts import ReviewFlagResult
from recon.tools import review_flag

pytestmark = pytest.mark.unit


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _FakeConnection:
    """Backs `review_flag`'s two queries with an in-memory dict keyed on
    `idempotency_key`, mirroring the real table's primary key.
    """

    def __init__(self, store: dict[str, dict[str, Any]]) -> None:
        self._store = store

    async def execute(self, sql: str, *params: Any) -> None:
        assert "CREATE TABLE" in sql

    async def fetchrow(self, sql: str, *params: Any) -> dict[str, Any] | None:
        if "INSERT INTO" in sql:
            key, case_id, reason, created_by, created_at = params
            if key in self._store:
                return None
            row = {
                "idempotency_key": key,
                "case_id": case_id,
                "reason": reason,
                "created_by": created_by,
                "created_at": created_at,
            }
            self._store[key] = row
            return row
        assert "SELECT" in sql
        (key,) = params
        return self._store.get(key)

    async def close(self) -> None:
        pass


@pytest.fixture
def fake_postgres(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    store: dict[str, dict[str, Any]] = {}

    async def fake_connect(database_url: str) -> _FakeConnection:
        return _FakeConnection(store)

    monkeypatch.setattr(review_flag.asyncpg, "connect", fake_connect)
    return store


async def _get_preview_token(
    case_id: str, reason: str, idempotency_key: str, created_by: str
) -> str:
    """The real flow: obtain a `preview_token` from an unconfirmed call,
    exactly as `prompts/supervisor.md`'s three-step protocol requires -
    never `review_flag._preview_token` directly, so these tests exercise the
    same path a real caller does.
    """
    preview = await review_flag.flag_case_for_review(
        None, case_id, reason, idempotency_key, created_by
    )
    assert preview.preview_token is not None
    return preview.preview_token


@pytest.mark.anyio
async def test_dry_run_returns_preview_without_connecting_to_postgres() -> None:
    result = await review_flag.flag_case_for_review(
        None,
        "case-001",
        "figure disagrees with source document",
        "key-001",
        "agent_sdk:single",
        dry_run=True,
    )

    assert result.status == "would_write"
    assert result.flag is not None
    assert result.flag.case_id == "case-001"
    assert result.flag.idempotency_key == "key-001"
    assert result.preview_token is None


@pytest.mark.anyio
async def test_unconfirmed_call_pauses_without_connecting_to_postgres() -> None:
    result = await review_flag.flag_case_for_review(
        None,
        "case-001",
        "figure disagrees with source document",
        "key-001",
        "agent_sdk:single",
    )

    assert result.status == "confirmation_required"
    assert result.flag is not None
    assert result.flag.idempotency_key == "key-001"
    assert result.preview_token is not None


@pytest.mark.anyio
async def test_confirmed_call_without_a_preceding_unconfirmed_call_is_refused() -> None:
    """The bug PR #43's round-3 review found: a `confirmed=True` call used
    to write immediately even as the very first call, with nothing checking
    that an unconfirmed preview ever happened. Now it must be refused.
    """
    with pytest.raises(RuntimeError, match="preview_token"):
        await review_flag.flag_case_for_review(
            "postgresql://unused",
            "case-001",
            "reason",
            "key-001",
            "agent_sdk:single",
            confirmed=True,
        )


@pytest.mark.anyio
async def test_confirmed_call_with_a_stale_or_wrong_token_is_refused(
    fake_postgres: dict[str, dict[str, Any]],
) -> None:
    token = await _get_preview_token(
        "case-001", "reason", "key-001", "agent_sdk:single"
    )

    with pytest.raises(RuntimeError, match="preview_token"):
        await review_flag.flag_case_for_review(
            "postgresql://unused",
            "case-001",
            "reason",
            "key-001",
            "agent_sdk:single",
            confirmed=True,
            preview_token=token + "-tampered",
        )
    assert len(fake_postgres) == 0


@pytest.mark.anyio
async def test_confirmed_call_without_database_url_raises() -> None:
    token = await _get_preview_token(
        "case-001", "reason", "key-001", "agent_sdk:single"
    )

    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        await review_flag.flag_case_for_review(
            None,
            "case-001",
            "reason",
            "key-001",
            "agent_sdk:single",
            confirmed=True,
            preview_token=token,
        )


@pytest.mark.anyio
async def test_confirmed_call_writes_one_row(
    fake_postgres: dict[str, dict[str, Any]],
) -> None:
    token = await _get_preview_token(
        "case-001",
        "figure disagrees with source document",
        "key-001",
        "agent_sdk:single",
    )

    result = await review_flag.flag_case_for_review(
        "postgresql://unused",
        "case-001",
        "figure disagrees with source document",
        "key-001",
        "agent_sdk:single",
        confirmed=True,
        preview_token=token,
    )

    assert result.status == "created"
    assert result.flag is not None
    assert result.flag.case_id == "case-001"
    assert result.preview_token is None
    assert len(fake_postgres) == 1


@pytest.mark.anyio
async def test_calling_twice_with_the_same_idempotency_key_produces_one_row(
    fake_postgres: dict[str, dict[str, Any]],
) -> None:
    token = await _get_preview_token(
        "case-001",
        "figure disagrees with source document",
        "key-001",
        "agent_sdk:single",
    )

    first = await review_flag.flag_case_for_review(
        "postgresql://unused",
        "case-001",
        "figure disagrees with source document",
        "key-001",
        "agent_sdk:single",
        confirmed=True,
        preview_token=token,
    )
    second = await review_flag.flag_case_for_review(
        "postgresql://unused",
        "case-001",
        "figure disagrees with source document",
        "key-001",
        "agent_sdk:single",
        confirmed=True,
        preview_token=token,
    )

    assert first.status == "created"
    assert second.status == "already_exists"
    assert len(fake_postgres) == 1
    assert first.flag == second.flag


@pytest.mark.anyio
async def test_different_idempotency_keys_produce_separate_rows(
    fake_postgres: dict[str, dict[str, Any]],
) -> None:
    token_a = await _get_preview_token(
        "case-001", "reason a", "key-001", "agent_sdk:single"
    )
    token_b = await _get_preview_token(
        "case-002", "reason b", "key-002", "agent_sdk:single"
    )

    await review_flag.flag_case_for_review(
        "postgresql://unused",
        "case-001",
        "reason a",
        "key-001",
        "agent_sdk:single",
        confirmed=True,
        preview_token=token_a,
    )
    await review_flag.flag_case_for_review(
        "postgresql://unused",
        "case-002",
        "reason b",
        "key-002",
        "agent_sdk:single",
        confirmed=True,
        preview_token=token_b,
    )

    assert len(fake_postgres) == 2


@pytest.mark.anyio
async def test_idempotency_key_collision_across_different_cases_is_flagged(
    fake_postgres: dict[str, dict[str, Any]],
) -> None:
    """idempotency_key is freeform LLM-chosen text, not derived from case_id
    by anything structural - two different cases could pick the same one.
    That must not silently report case-002's caller as "already_exists"
    against case-001's row without saying so.
    """
    token_a = await _get_preview_token(
        "case-001", "reason a", "shared-key", "agent_sdk:single"
    )
    token_b = await _get_preview_token(
        "case-002", "reason b", "shared-key", "agent_sdk:single"
    )

    first = await review_flag.flag_case_for_review(
        "postgresql://unused",
        "case-001",
        "reason a",
        "shared-key",
        "agent_sdk:single",
        confirmed=True,
        preview_token=token_a,
    )
    second = await review_flag.flag_case_for_review(
        "postgresql://unused",
        "case-002",
        "reason b",
        "shared-key",
        "agent_sdk:single",
        confirmed=True,
        preview_token=token_b,
    )

    assert first.status == "created"
    assert second.status == "already_exists"
    assert second.flag is not None
    assert second.flag.case_id == "case-001"  # the row that actually exists
    assert "different case" in second.message
    assert "case-002" in second.message
    assert len(fake_postgres) == 1


def test_review_flag_result_model_accepts_every_documented_status() -> None:
    for status in ("would_write", "confirmation_required", "created", "already_exists"):
        ReviewFlagResult(status=status, flag=None, message="ok", preview_token=None)  # type: ignore[arg-type]
