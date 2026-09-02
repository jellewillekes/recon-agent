"""MCP server over stdio exposing read-only, DuckDB-backed research tools.

Every tool returns a `ToolResult` (`docs/contracts.md` section 3) and covers
all five statuses. Data comes from the synthetic fixture in `fixtures.py`,
not from real filings — see `docs/data-sources.md` for what that means for
answer quality against finance-agent-bench.

Tool functions below take the DuckDB connection as their first argument and
are called directly in tests, with no MCP transport and no LLM involved. The
`build_server` / `main` at the bottom wire the same functions to an
`MCPServer` for real stdio use.
"""

import concurrent.futures
import time
from typing import Any, Literal

import duckdb
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field, ValidationError

from recon.contracts import ToolResult
from recon.tools.fixtures import seed

MAX_ROWS = 500
TIMEOUT_S = 30.0

_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4)


class _ToolUnavailable(Exception):
    """Raised by `_run_bounded`, caught by each tool to build an `unavailable` result."""


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _execute_and_fetch(
    conn: duckdb.DuckDBPyConnection, sql: str, params: list[Any]
) -> tuple[list[str], list[tuple[Any, ...]]]:
    cursor = conn.execute(sql, params)
    columns = [d[0] for d in cursor.description]
    return columns, cursor.fetchall()


def _run_bounded(
    conn: duckdb.DuckDBPyConnection, sql: str, params: list[Any]
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Run `sql` on `conn`, converting a DuckDB error or a TIMEOUT_S timeout
    into `_ToolUnavailable` instead of letting either reach the caller."""
    future = _EXECUTOR.submit(_execute_and_fetch, conn, sql, params)
    try:
        return future.result(timeout=TIMEOUT_S)
    except concurrent.futures.TimeoutError as exc:
        raise _ToolUnavailable(
            f"Query exceeded the {TIMEOUT_S}s timeout. Narrow the request and retry."
        ) from exc
    except duckdb.Error as exc:
        raise _ToolUnavailable(
            f"Query failed: {exc}. The data source may be temporarily unavailable — "
            "retrying is worth trying once."
        ) from exc


def _rows_to_dicts(
    columns: list[str], rows: list[tuple[Any, ...]]
) -> list[dict[str, Any]]:
    return [dict(zip(columns, row, strict=True)) for row in rows]


def _company_exists(conn: duckdb.DuckDBPyConnection, company_id: str) -> bool:
    _columns, rows = _run_bounded(
        conn, "SELECT 1 FROM companies WHERE company_id = ?", [company_id]
    )
    return len(rows) > 0


class ListCompaniesInput(BaseModel):
    sector: str | None = Field(default=None, min_length=1)


class ListFinancialConceptsInput(BaseModel):
    company_id: str = Field(min_length=1)


class GetFinancialFactInput(BaseModel):
    company_id: str = Field(min_length=1)
    concept: str = Field(min_length=1)
    fiscal_year: int | None = Field(default=None, ge=1900, le=2100)
    fiscal_period: Literal["FY", "Q1", "Q2", "Q3", "Q4"] | None = None


class SearchFilingsInput(BaseModel):
    company_id: str = Field(min_length=1)
    keyword: str | None = Field(default=None, min_length=1)
    form_type: Literal["10-K", "10-Q", "8-K"] | None = None
    fiscal_year: int | None = Field(default=None, ge=1900, le=2100)


def list_companies(
    conn: duckdb.DuckDBPyConnection, sector: str | None = None
) -> ToolResult:
    """List known companies, optionally filtered by sector.

    Use this first to discover which `company_id`s exist — every other tool
    needs one. Don't use it to look up a single company by name; there's no
    name search here, only an exact sector filter.
    """
    start = time.perf_counter()
    try:
        validated = ListCompaniesInput(sector=sector)
    except ValidationError as exc:
        return ToolResult(
            status="invalid_input",
            data=[],
            row_count=0,
            message=f"Invalid input: {exc.errors()[0]['msg']}. `sector`, if given, must "
            "be a non-empty string.",
            elapsed_ms=_elapsed_ms(start),
        )

    try:
        if validated.sector is None:
            columns, rows = _run_bounded(
                conn,
                "SELECT company_id, name, sector, fiscal_year_end FROM companies",
                [],
            )
        else:
            columns, rows = _run_bounded(
                conn,
                "SELECT company_id, name, sector, fiscal_year_end FROM companies "
                "WHERE sector = ?",
                [validated.sector],
            )
    except _ToolUnavailable as exc:
        return ToolResult(
            status="unavailable",
            data=[],
            row_count=0,
            message=str(exc),
            elapsed_ms=_elapsed_ms(start),
        )

    if not rows:
        return ToolResult(
            status="empty",
            data=[],
            row_count=0,
            message=f"No companies found for sector={validated.sector!r}. Call with no "
            "sector to see everything available.",
            elapsed_ms=_elapsed_ms(start),
        )

    data = _rows_to_dicts(columns, rows)
    if len(data) > MAX_ROWS:
        return ToolResult(
            status="truncated",
            data=data[:MAX_ROWS],
            row_count=len(data),
            message=f"{len(data)} companies match, showing the first {MAX_ROWS}. "
            "Narrow with `sector` to see the rest.",
            elapsed_ms=_elapsed_ms(start),
        )

    return ToolResult(
        status="ok",
        data=data,
        row_count=len(data),
        message=f"{len(data)} companies.",
        elapsed_ms=_elapsed_ms(start),
    )


def list_financial_concepts(
    conn: duckdb.DuckDBPyConnection, company_id: str
) -> ToolResult:
    """List which financial concepts (line items) exist for a company.

    Call this before `get_financial_fact` — concept names aren't guessable,
    and passing one this tool didn't return will come back `empty`, not an
    error.
    """
    start = time.perf_counter()
    try:
        validated = ListFinancialConceptsInput(company_id=company_id)
    except ValidationError as exc:
        return ToolResult(
            status="invalid_input",
            data=[],
            row_count=0,
            message=f"Invalid input: {exc.errors()[0]['msg']}. `company_id` must be a "
            "non-empty string.",
            elapsed_ms=_elapsed_ms(start),
        )

    try:
        if not _company_exists(conn, validated.company_id):
            return ToolResult(
                status="invalid_input",
                data=[],
                row_count=0,
                message=f"Unknown company_id {validated.company_id!r}. Call "
                "list_companies for valid ids.",
                elapsed_ms=_elapsed_ms(start),
            )
        columns, rows = _run_bounded(
            conn,
            "SELECT DISTINCT concept FROM financial_facts WHERE company_id = ? "
            "ORDER BY concept",
            [validated.company_id],
        )
    except _ToolUnavailable as exc:
        return ToolResult(
            status="unavailable",
            data=[],
            row_count=0,
            message=str(exc),
            elapsed_ms=_elapsed_ms(start),
        )

    if not rows:
        return ToolResult(
            status="empty",
            data=[],
            row_count=0,
            message=f"No financial concepts recorded for {validated.company_id!r}.",
            elapsed_ms=_elapsed_ms(start),
        )

    data = _rows_to_dicts(columns, rows)
    if len(data) > MAX_ROWS:
        return ToolResult(
            status="truncated",
            data=data[:MAX_ROWS],
            row_count=len(data),
            message=f"{len(data)} concepts match, showing the first {MAX_ROWS}.",
            elapsed_ms=_elapsed_ms(start),
        )

    return ToolResult(
        status="ok",
        data=data,
        row_count=len(data),
        message=f"{len(data)} concepts for {validated.company_id!r}.",
        elapsed_ms=_elapsed_ms(start),
    )


def get_financial_fact(
    conn: duckdb.DuckDBPyConnection,
    company_id: str,
    concept: str,
    fiscal_year: int | None = None,
    fiscal_period: Literal["FY", "Q1", "Q2", "Q3", "Q4"] | None = None,
) -> ToolResult:
    """Look up a financial concept's value for a company.

    `concept` must come from `list_financial_concepts` — don't guess a name.
    Omit `fiscal_year`/`fiscal_period` to get every recorded period, e.g. for
    a trend question.
    """
    start = time.perf_counter()
    try:
        validated = GetFinancialFactInput(
            company_id=company_id,
            concept=concept,
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
        )
    except ValidationError as exc:
        error = exc.errors()[0]
        return ToolResult(
            status="invalid_input",
            data=[],
            row_count=0,
            message=f"Invalid input on {error['loc'][0]}: {error['msg']}.",
            elapsed_ms=_elapsed_ms(start),
        )

    try:
        if not _company_exists(conn, validated.company_id):
            return ToolResult(
                status="invalid_input",
                data=[],
                row_count=0,
                message=f"Unknown company_id {validated.company_id!r}. Call "
                "list_companies for valid ids.",
                elapsed_ms=_elapsed_ms(start),
            )

        conditions = ["company_id = ?", "concept = ?"]
        params: list[Any] = [validated.company_id, validated.concept]
        if validated.fiscal_year is not None:
            conditions.append("fiscal_year = ?")
            params.append(validated.fiscal_year)
        if validated.fiscal_period is not None:
            conditions.append("fiscal_period = ?")
            params.append(validated.fiscal_period)

        columns, rows = _run_bounded(
            conn,
            "SELECT fiscal_year, fiscal_period, concept, value, unit FROM financial_facts "
            f"WHERE {' AND '.join(conditions)} ORDER BY fiscal_year, fiscal_period",
            params,
        )
    except _ToolUnavailable as exc:
        return ToolResult(
            status="unavailable",
            data=[],
            row_count=0,
            message=str(exc),
            elapsed_ms=_elapsed_ms(start),
        )

    if not rows:
        return ToolResult(
            status="empty",
            data=[],
            row_count=0,
            message=f"No {validated.concept!r} fact for {validated.company_id!r} with "
            "the given filters. Try list_financial_concepts, or drop fiscal_year/"
            "fiscal_period to widen the search.",
            elapsed_ms=_elapsed_ms(start),
        )

    data = _rows_to_dicts(columns, rows)
    if len(data) > MAX_ROWS:
        return ToolResult(
            status="truncated",
            data=data[:MAX_ROWS],
            row_count=len(data),
            message=f"{len(data)} facts match, showing the first {MAX_ROWS}. Narrow "
            "with fiscal_year or fiscal_period.",
            elapsed_ms=_elapsed_ms(start),
        )

    return ToolResult(
        status="ok",
        data=data,
        row_count=len(data),
        message=f"{len(data)} fact(s) for {validated.concept!r} on "
        f"{validated.company_id!r}.",
        elapsed_ms=_elapsed_ms(start),
    )


def search_filings(
    conn: duckdb.DuckDBPyConnection,
    company_id: str,
    keyword: str | None = None,
    form_type: Literal["10-K", "10-Q", "8-K"] | None = None,
    fiscal_year: int | None = None,
) -> ToolResult:
    """Search filing summaries for a company by keyword, form type, or year.

    This searches short filing summaries, not full document text — it can't
    answer questions that need a specific page or exhibit. Omit `keyword` to
    list everything on file for the company.
    """
    start = time.perf_counter()
    try:
        validated = SearchFilingsInput(
            company_id=company_id,
            keyword=keyword,
            form_type=form_type,
            fiscal_year=fiscal_year,
        )
    except ValidationError as exc:
        error = exc.errors()[0]
        return ToolResult(
            status="invalid_input",
            data=[],
            row_count=0,
            message=f"Invalid input on {error['loc'][0]}: {error['msg']}.",
            elapsed_ms=_elapsed_ms(start),
        )

    try:
        if not _company_exists(conn, validated.company_id):
            return ToolResult(
                status="invalid_input",
                data=[],
                row_count=0,
                message=f"Unknown company_id {validated.company_id!r}. Call "
                "list_companies for valid ids.",
                elapsed_ms=_elapsed_ms(start),
            )

        conditions = ["company_id = ?"]
        params: list[Any] = [validated.company_id]
        if validated.keyword is not None:
            conditions.append("summary_text ILIKE ?")
            params.append(f"%{validated.keyword}%")
        if validated.form_type is not None:
            conditions.append("form_type = ?")
            params.append(validated.form_type)
        if validated.fiscal_year is not None:
            conditions.append("fiscal_year = ?")
            params.append(validated.fiscal_year)

        columns, rows = _run_bounded(
            conn,
            "SELECT form_type, fiscal_year, fiscal_period, filed_date, summary_text "
            f"FROM filings WHERE {' AND '.join(conditions)} ORDER BY filed_date",
            params,
        )
    except _ToolUnavailable as exc:
        return ToolResult(
            status="unavailable",
            data=[],
            row_count=0,
            message=str(exc),
            elapsed_ms=_elapsed_ms(start),
        )

    if not rows:
        return ToolResult(
            status="empty",
            data=[],
            row_count=0,
            message=f"No filings match for {validated.company_id!r} with the given "
            "filters. Try dropping keyword/form_type/fiscal_year to widen the search.",
            elapsed_ms=_elapsed_ms(start),
        )

    data = _rows_to_dicts(columns, rows)
    if len(data) > MAX_ROWS:
        return ToolResult(
            status="truncated",
            data=data[:MAX_ROWS],
            row_count=len(data),
            message=f"{len(data)} filings match, showing the first {MAX_ROWS}. Narrow "
            "with keyword, form_type, or fiscal_year.",
            elapsed_ms=_elapsed_ms(start),
        )

    return ToolResult(
        status="ok",
        data=data,
        row_count=len(data),
        message=f"{len(data)} filing(s) for {validated.company_id!r}.",
        elapsed_ms=_elapsed_ms(start),
    )


def build_server(conn: duckdb.DuckDBPyConnection) -> MCPServer:
    """Wire the tool functions above to an `MCPServer` bound to `conn`."""
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
