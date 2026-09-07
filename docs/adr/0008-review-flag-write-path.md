# ADR 0008: `flag_case_for_review` write path — result shape, confirmation, schema bootstrap

Status: Accepted
Date: 2026-09-07

## Context

Issue #13 (Step 8) asks for the write path `docs/contracts.md` section 6 has described
since step 1 but never implemented: Postgres-backed, an `idempotency_key` unique
constraint so calling twice produces one row, a dry-run mode, a real pause for
confirmation before executing, and supervisor-only access enforced the same structural
way as step 7's tool restriction.

Three design questions this ADR settles:

**Result shape.** `docs/contracts.md` section 2 already rules out reusing `ToolResult`:
"a tool that needs to write is not a tool — it is a state operation." `ToolResult`'s five
read statuses (`ok/empty/truncated/invalid_input/unavailable`) don't describe a write's
real states — a preview, a paused-for-confirmation state, a fresh write, and a write that
already happened are not the same thing as "query succeeded/was empty/was too big."
Added `ReviewFlagResult` (`contracts.py`, documented in `docs/contracts.md` section 6)
with its own four-value `status`: `would_write / confirmation_required / created /
already_exists`.

**Confirmation mechanism.** There's no human-in-the-loop channel yet — LangGraph's
interrupt primitive is step 9, a different runtime. Given `flag_case_for_review` is
invoked by the agent itself as an MCP tool, the only real pause available is structural:
require two separate tool calls before anything writes. `dry_run=True` returns a preview
and never touches Postgres. Otherwise, a call without `confirmed=True` also never touches
Postgres — it returns the same preview with `status="confirmation_required"`, forcing a
second, explicit call with `confirmed=True` to actually write. This is a genuine pause in
the run (nothing writes within one call, ever) rather than a log line, and it's fully
testable without any human or extra channel.

**Schema bootstrap.** No migration tooling exists in the project, and adding one
(Alembic or similar) needs approval as a new dependency (`CLAUDE.md`). `review_flag.py`
issues its own idempotent `CREATE TABLE IF NOT EXISTS review_flags (...)` the first time a
confirmed write is attempted, mirroring `api/health.py:check_postgres`'s existing
"connect per call with `asyncpg.connect`, no pool" simplicity — call volume here is one
row per escalated case, not a hot path. `idempotency_key` is the table's `PRIMARY KEY`
directly (not a separate id column plus a `UNIQUE` constraint) — a primary key already
implies `UNIQUE NOT NULL`, and there's no other natural key for this table.

## Decision

`src/recon/tools/review_flag.py::flag_case_for_review(database_url, case_id, reason,
idempotency_key, created_by, *, dry_run=False, confirmed=False) -> ReviewFlagResult`,
wired as `flag_case_for_review_tool` in `tools/mcp_server.py`. The actual write is
`INSERT INTO review_flags (...) VALUES (...) ON CONFLICT (idempotency_key) DO NOTHING
RETURNING *`; a `None` return (conflict) falls back to a `SELECT` for the existing row and
reports `already_exists`. This is what makes "call twice with the same key, one row" a
property of the SQL, not application-level de-duplication.

Supervisor-only access uses the exact mechanism ADR-0007 already established: `config/
roles.yaml`'s `supervisor.tools` gains `flag_case_for_review`, which flows through
`multi_agent._build_role_options` into the supervisor's `ClaudeAgentOptions.allowed_tools`
— the tool is structurally absent from both workers' clients (their own `tools:` lists are
untouched) and from the critic (which still gets no `mcp_servers` at all). Single mode has
no worker/supervisor split — the one agent acts as supervisor — so `agent_sdk.ALLOWED_TOOLS`
gains the tool too.

`ReviewFlag.created_by` ("runtime + mode" per its docstring) is populated from a
`RECON_CREATED_BY` environment variable set on the MCP subprocess's environment by whichever
runtime spawns it (`_build_options` for single mode, `_build_role_options` for multi mode),
merged with the full parent environment (`{**os.environ, "RECON_CREATED_BY": ...}`) rather
than replacing it — this avoids depending on undocumented behavior of whether the SDK's
`McpStdioServerConfig.env` merges with or replaces the parent process's environment. The
alternative — having the agent pass `created_by` as a tool argument — was rejected: it would
make a value meant to record *how* the write happened dependent on the agent correctly
self-reporting it, rather than being structurally determined by which runtime is running.

## Consequences

- A write requires `DATABASE_URL` configured and Postgres reachable; the preview and
  confirmation-pause calls (`dry_run=True`, or the unconfirmed call) work regardless,
  since neither touches the database. A confirmed write against an unreachable Postgres
  raises — there's no `ReviewFlagResult` status for "write failed," unlike `ToolResult`'s
  `unavailable`. This is a deliberate gap: the issue's requirements don't describe a
  write-side outage case, and inventing one now would be speculative.
- No live Postgres exists anywhere yet (`docker/compose.yaml` is step 10) — this path is
  exercised in tests against a fake `asyncpg` connection (`tests/test_review_flag.py`),
  not a real database. It has not been run against real Postgres.
- The schema lives only in this module's DDL string, not a tracked migration file. If the
  table's shape ever needs to change, that change has to be written as an `ALTER TABLE`
  here too — there's no migration history to replay against an existing deployment.
