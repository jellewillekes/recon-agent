# ADR 0003: Approve fastapi, uvicorn, prometheus-client, asyncpg

Status: Accepted
Date: 2026-09-07

## Context

Issue #11 / implementation-plan.md Step 6 required building `src/recon/api/` with FastAPI,
a `GET /metrics` endpoint in Prometheus text format, and a `GET /readyz` check reaching
Postgres. None of `fastapi`, `uvicorn`, `prometheus-client`, `asyncpg` existed in
`pyproject.toml`. `CLAUDE.md`'s "Forbidden without explicit permission" list requires new
dependencies to be proposed, with installing gated on approval. PR #31's round-2 review
flagged the four additions as worth explicit sign-off rather than assuming it was implicit
in the issue.

## Decision

Approve all four:

- `fastapi`, `uvicorn` — required directly by the issue ("Build `src/recon/api/` with
  FastAPI"; `uvicorn` is already named in `CLAUDE.md`'s own Commands section).
- `prometheus-client` — the standard, minimal library for the required Prometheus text
  format; hand-rolling that format would be more code for no benefit.
- `asyncpg` — the async-native Postgres driver for the `/readyz` check, matching this
  async framework (over `psycopg`, which is sync and would need thread-offloading here).

## Consequences

`pyproject.toml`/`uv.lock` carry these four going forward. Any future Postgres-touching
code (Step 8's write path) should default to `asyncpg` too, rather than introducing a
second driver for the same database.
