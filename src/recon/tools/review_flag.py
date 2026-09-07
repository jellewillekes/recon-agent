"""The write path: `flag_case_for_review` (`docs/contracts.md` section 6).

Not a `ToolResult`-shaped read tool — a state operation, backed by Postgres,
wired to the supervisor role only (see `config/roles.yaml` and
`agent_sdk.ALLOWED_TOOLS`). `mcp_server.py` wires this function to an
`MCPServer` the same way it wires the read tools in `server.py`; called
directly here for tests, no MCP transport involved.

No migration tooling in the project (adding one needs approval per
CLAUDE.md) — `_ensure_schema` issues its own idempotent DDL the first time a
write is attempted, mirroring `api/health.py:check_postgres`'s "connect per
call, no pool" simplicity. Call volume is low: one flag per escalated case.
"""

from datetime import UTC, datetime

import asyncpg

from recon.contracts import ReviewFlag, ReviewFlagResult

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS review_flags (
    idempotency_key TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
)
"""

_INSERT_SQL = """
INSERT INTO review_flags (idempotency_key, case_id, reason, created_by, created_at)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (idempotency_key) DO NOTHING
RETURNING idempotency_key, case_id, reason, created_by, created_at
"""

_SELECT_SQL = """
SELECT idempotency_key, case_id, reason, created_by, created_at
FROM review_flags
WHERE idempotency_key = $1
"""


def _flag_from_row(row: asyncpg.Record) -> ReviewFlag:
    return ReviewFlag(
        idempotency_key=row["idempotency_key"],
        case_id=row["case_id"],
        reason=row["reason"],
        created_by=row["created_by"],
        created_at=row["created_at"],
    )


async def flag_case_for_review(
    database_url: str | None,
    case_id: str,
    reason: str,
    idempotency_key: str,
    created_by: str,
    *,
    dry_run: bool = False,
    confirmed: bool = False,
) -> ReviewFlagResult:
    """Flag `case_id` for human review, per the rules in `docs/contracts.md`
    section 6.

    `dry_run=True` and the default `confirmed=False` never touch Postgres —
    only a call with `confirmed=True` writes, and needs `database_url`
    configured and reachable. Calling this twice with the same
    `idempotency_key`, both times `confirmed=True`, produces exactly one row:
    the second call's `INSERT ... ON CONFLICT DO NOTHING` inserts nothing and
    the fallback `SELECT` reports the row the first call created.
    """
    preview = ReviewFlag(
        idempotency_key=idempotency_key,
        case_id=case_id,
        reason=reason,
        created_by=created_by,
        created_at=datetime.now(UTC),
    )

    if dry_run:
        return ReviewFlagResult(
            status="would_write",
            flag=preview,
            message="Dry run: this write was not performed.",
        )

    if not confirmed:
        return ReviewFlagResult(
            status="confirmation_required",
            flag=preview,
            message="Call again with confirmed=True to write this flag.",
        )

    if not database_url:
        raise RuntimeError("DATABASE_URL not configured; cannot write a review flag.")

    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute(_CREATE_TABLE_SQL)
        inserted = await conn.fetchrow(
            _INSERT_SQL,
            idempotency_key,
            case_id,
            reason,
            created_by,
            preview.created_at,
        )
        if inserted is not None:
            return ReviewFlagResult(
                status="created",
                flag=_flag_from_row(inserted),
                message="Review flag created.",
            )
        existing = await conn.fetchrow(_SELECT_SQL, idempotency_key)
        if existing is None:
            raise RuntimeError(
                f"idempotency_key {idempotency_key!r} conflicted on insert but no "
                "row was found on the follow-up read."
            )
        existing_flag = _flag_from_row(existing)
        # idempotency_key is freeform text an agent chooses, not derived from
        # case_id by anything structural - two different cases could pick the
        # same key. That's not this function's job to prevent (nothing here
        # can tell "same case, retried" from "different case, collided"
        # apart), but a caller silently told "already_exists" for a case that
        # isn't theirs needs to know, not just get back someone else's row.
        if existing_flag.case_id != case_id:
            return ReviewFlagResult(
                status="already_exists",
                flag=existing_flag,
                message=(
                    f"idempotency_key {idempotency_key!r} already exists but for a "
                    f"different case ({existing_flag.case_id!r}, not {case_id!r}) - "
                    "likely a collision, not a retry. No write was performed for "
                    "this case; call again with a more specific idempotency_key."
                ),
            )
        return ReviewFlagResult(
            status="already_exists",
            flag=existing_flag,
            message="A review flag with this idempotency_key already exists.",
        )
    finally:
        await conn.close()
