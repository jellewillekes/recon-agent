"""Wires the tool functions in `server.py` (and the write path in
`review_flag.py`) to an `MCPServer` over stdio.

Kept separate from `server.py` so that module stays focused on the tool
contract itself; nothing here is exercised by `tests/test_tools.py`, which
calls the plain functions directly.

Uses `mcp.server.fastmcp.FastMCP`, not `mcp.server.mcpserver.MCPServer` (the
name in `mcp>=2.0`) - this project pins `mcp<2.0` (issue #14) because
`langchain-mcp-adapters` doesn't support `mcp` 2.x yet. Same `@server.tool()`
decorator shape either way; see `docs/adr/0010-langgraph-runtime.md`.
"""

import os
from typing import Any, Literal

import duckdb
from mcp.server.fastmcp import FastMCP

from recon.tools.fixtures import seed
from recon.tools.review_flag import flag_case_for_review
from recon.tools.server import (
    get_financial_fact,
    list_companies,
    list_financial_concepts,
    search_filings,
)


async def _call_flag_case_for_review(
    case_id: str,
    reason: str,
    idempotency_key: str,
    dry_run: bool,
    confirmed: bool,
    preview_token: str | None,
) -> dict[str, Any]:
    """The env-var-reading part of `flag_case_for_review_tool`, pulled out
    of the `@server.tool()` closure so it's directly callable in a test - the
    closure itself isn't exercised by anything, per this module's own
    docstring, so a typo'd `RECON_CREATED_BY`/`DATABASE_URL` name on either
    runtime's side would otherwise fall back silently with nothing to catch it.
    """
    created_by = os.environ.get("RECON_CREATED_BY", "agent_sdk:unknown")
    result = await flag_case_for_review(
        os.environ.get("DATABASE_URL"),
        case_id,
        reason,
        idempotency_key,
        created_by,
        dry_run=dry_run,
        confirmed=confirmed,
        preview_token=preview_token,
    )
    return result.model_dump(mode="json")


def build_server(conn: duckdb.DuckDBPyConnection) -> FastMCP:
    """Wire the tool functions in `server.py` to an `MCPServer` bound to `conn`."""
    server = FastMCP(name="recon-tools")

    @server.tool()
    def list_companies_tool(sector: str | None = None) -> dict[str, Any]:
        """List known companies, optionally filtered by sector."""
        return list_companies(conn, sector).model_dump()

    @server.tool()
    def list_financial_concepts_tool(company_id: str) -> dict[str, Any]:
        """List which financial concepts exist for a company."""
        return list_financial_concepts(conn, company_id).model_dump()

    @server.tool()
    def get_financial_fact_tool(
        company_id: str,
        concept: str,
        fiscal_year: int | None = None,
        fiscal_period: Literal["FY", "Q1", "Q2", "Q3", "Q4"] | None = None,
    ) -> dict[str, Any]:
        """Look up a financial concept's value for a company."""
        return get_financial_fact(
            conn, company_id, concept, fiscal_year, fiscal_period
        ).model_dump()

    @server.tool()
    def search_filings_tool(
        company_id: str,
        keyword: str | None = None,
        form_type: Literal["10-K", "10-Q", "8-K"] | None = None,
        fiscal_year: int | None = None,
    ) -> dict[str, Any]:
        """Search filing summaries for a company."""
        return search_filings(
            conn, company_id, keyword, form_type, fiscal_year
        ).model_dump()

    @server.tool()
    async def flag_case_for_review_tool(
        case_id: str,
        reason: str,
        idempotency_key: str,
        dry_run: bool = False,
        confirmed: bool = False,
        preview_token: str | None = None,
    ) -> dict[str, Any]:
        """Flag a case for human review. Supervisor only (docs/contracts.md
        section 6) — restricted structurally via `config/roles.yaml` and
        `agent_sdk.ALLOWED_TOOLS`, not by this tool refusing a caller.

        Call with `dry_run=True` to preview the write without performing it.
        Otherwise call once to see the preview, pause for confirmation, and
        get a `preview_token` back — then call a third time with
        `confirmed=True` and that exact `preview_token` to actually write it.
        `confirmed=True` without the matching token from a prior call is
        refused; the pause cannot be skipped.
        """
        return await _call_flag_case_for_review(
            case_id, reason, idempotency_key, dry_run, confirmed, preview_token
        )

    return server


def main() -> None:
    conn = duckdb.connect(":memory:")
    seed(conn)
    server = build_server(conn)
    server.run()


if __name__ == "__main__":
    main()
