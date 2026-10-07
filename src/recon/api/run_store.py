"""Saved research runs, in Postgres (ADR 0030, #115).

A run is transactional state, so it belongs in Postgres under the storage
boundary. Like `tools/review_flag.py` there is no migration tooling: each call
connects, and the first write creates the table with `CREATE TABLE IF NOT
EXISTS`. Call volume is one write per answered request.
"""

import json
from datetime import UTC, datetime
from typing import Any

import asyncpg

from recon.api.schemas import Feedback, ResearchRun, RunSummary, StoredFeedback

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS research_runs (
    run_id TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL,
    question TEXT NOT NULL,
    mode TEXT NOT NULL,
    run JSONB NOT NULL,
    feedback JSONB
)
"""

# A request ID a client reuses keeps the first run saved under it.
_INSERT_SQL = """
INSERT INTO research_runs (run_id, created_at, question, mode, run)
VALUES ($1, $2, $3, $4, $5::jsonb)
ON CONFLICT (run_id) DO NOTHING
"""

_LIST_SQL = """
SELECT run, feedback FROM research_runs
ORDER BY created_at DESC, run_id DESC
LIMIT $1 OFFSET $2
"""

_GET_SQL = "SELECT run, feedback FROM research_runs WHERE run_id = $1"

_FEEDBACK_SQL = """
UPDATE research_runs SET feedback = $2::jsonb WHERE run_id = $1 RETURNING run_id
"""


# Seconds to wait for Postgres. Saving happens after the answer exists, so an
# unreachable database mustn't hold the response for asyncpg's default 60 s.
_CONNECT_TIMEOUT_S = 3.0


async def _connect(database_url: str) -> Any:
    conn = await asyncpg.connect(database_url, timeout=_CONNECT_TIMEOUT_S)
    await conn.execute(_CREATE_TABLE_SQL)
    return conn


def _from_row(row: Any) -> ResearchRun:
    """asyncpg returns JSONB as text unless a codec is set; accept both."""
    run, feedback = row["run"], row["feedback"]
    data = json.loads(run) if isinstance(run, str) else dict(run)
    if feedback is not None:
        data["feedback"] = (
            json.loads(feedback) if isinstance(feedback, str) else feedback
        )
    return ResearchRun.model_validate(data)


def summarize(run: ResearchRun) -> RunSummary:
    """The `GET /runs` row for `run`."""
    result = run.result
    return RunSummary(
        run_id=run.run_id,
        created_at=run.created_at,
        question=run.question,
        mode=run.mode,
        confidence=result.confidence,
        cost_eur=result.cost_eur,
        elapsed_ms=result.elapsed_ms,
        failed=result.error is not None and not result.answer.strip(),
        claim_count=len(result.claims),
        verified_evidence_count=sum(1 for e in result.evidence_items if e.verified),
        feedback=run.feedback.rating if run.feedback else None,
    )


async def save_run(database_url: str, run: ResearchRun) -> None:
    """Store `run`. Raises asyncpg or OS errors; the caller decides whether
    a failed save matters."""
    conn = await _connect(database_url)
    try:
        await conn.execute(
            _INSERT_SQL,
            run.run_id,
            run.created_at,
            run.question,
            run.mode,
            run.model_dump_json(exclude={"feedback"}),
        )
    finally:
        await conn.close()


async def list_runs(database_url: str, limit: int, offset: int) -> list[RunSummary]:
    """Saved runs, newest first."""
    conn = await _connect(database_url)
    try:
        rows = await conn.fetch(_LIST_SQL, limit, offset)
    finally:
        await conn.close()
    return [summarize(_from_row(row)) for row in rows]


async def get_run(database_url: str, run_id: str) -> ResearchRun | None:
    """One saved run, or None when there's no run with that id."""
    conn = await _connect(database_url)
    try:
        row = await conn.fetchrow(_GET_SQL, run_id)
    finally:
        await conn.close()
    return None if row is None else _from_row(row)


async def set_feedback(database_url: str, run_id: str, feedback: Feedback) -> bool:
    """Store `feedback` on a run, replacing any earlier one. False when
    there's no run with that id."""
    stored = StoredFeedback(**feedback.model_dump(), created_at=datetime.now(UTC))
    conn = await _connect(database_url)
    try:
        row = await conn.fetchrow(_FEEDBACK_SQL, run_id, stored.model_dump_json())
    finally:
        await conn.close()
    return row is not None
