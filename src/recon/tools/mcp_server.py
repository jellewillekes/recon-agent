"""Wires the tool functions in `server.py` to an `MCPServer` over stdio.

Kept separate from `server.py` so that module stays focused on the tool
contract itself; nothing here is exercised by `tests/test_tools.py`, which
calls the plain functions directly.
"""

from typing import Any, Literal

import duckdb
from mcp.server.mcpserver import MCPServer

from recon.tools.fixtures import seed
from recon.tools.server import (
    get_financial_fact,
    list_companies,
    list_financial_concepts,
    search_filings,
)


def build_server(conn: duckdb.DuckDBPyConnection) -> MCPServer:
    """Wire the tool functions in `server.py` to an `MCPServer` bound to `conn`."""
    server = MCPServer(name="recon-tools")

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

    return server


def main() -> None:
    conn = duckdb.connect(":memory:")
    seed(conn)
    server = build_server(conn)
    server.run()


if __name__ == "__main__":
    main()
