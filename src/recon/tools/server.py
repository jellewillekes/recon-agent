"""Read-only, DuckDB-backed research tools.

Every tool returns a `ToolResult` (`docs/contracts.md` section 3) and covers
all five statuses. Data comes from a normalized SEC EDGAR snapshot or the
synthetic fixture in `fixtures.py`, chosen by `data_source.py`. Both share
one schema, so the queries below serve either — see `docs/data-sources.md`.
How each query runs (timeout, retry, circuit breaker) and how its outcome maps
onto a status lives in `execution.py`.

Tool functions below take the DuckDB connection as their first argument and
are called directly in tests, with no MCP transport and no LLM involved.
`mcp_server.py` wires the same functions to an `MCPServer` for real stdio use.
"""

import time
from typing import Any, Literal

import duckdb
from pydantic import BaseModel, Field, ValidationError

from recon.contracts import ToolResult
from recon.tools.execution import check_company, failure, run_and_classify

FormType = Literal[
    "10-K",
    "10-K/A",
    "10-Q",
    "10-Q/A",
    "8-K",
    "8-K/A",
    "DEF 14A",
    "20-F",
    "20-F/A",
    "6-K",
    "40-F",
]


class ListCompaniesInput(BaseModel):
    sector: str | None = Field(default=None, min_length=1)
    query: str | None = Field(default=None, min_length=1)


class ListFinancialConceptsInput(BaseModel):
    company_id: str = Field(min_length=1)
    keyword: str | None = Field(default=None, min_length=1)


class GetFinancialFactInput(BaseModel):
    company_id: str = Field(min_length=1)
    concept: str = Field(min_length=1)
    fiscal_year: int | None = Field(default=None, ge=1900, le=2100)
    fiscal_period: Literal["FY", "Q1", "Q2", "Q3", "Q4"] | None = None


class SearchFilingsInput(BaseModel):
    company_id: str = Field(min_length=1)
    keyword: str | None = Field(default=None, min_length=1)
    form_type: FormType | None = None
    fiscal_year: int | None = Field(default=None, ge=1900, le=2100)


def _validate[M: BaseModel](
    model: type[M], start: float, hint: str | None, **fields: Any
) -> M | ToolResult:
    """`model(**fields)`, or an `invalid_input` result for its first error.

    Without a `hint`, the message names the offending field instead.
    """
    try:
        return model(**fields)
    except ValidationError as exc:
        error = exc.errors()[0]
    if hint is None:
        message = f"Invalid input on {error['loc'][0]}: {error['msg']}."
    else:
        message = f"Invalid input: {error['msg']}. {hint}"
    return failure(start, "invalid_input", message)


def _where(conditions: list[tuple[str, Any]]) -> tuple[str, list[Any]]:
    """AND together `(sql, param)` pairs, skipping any whose param is None.

    A condition with two `?` placeholders gets its param twice.
    """
    active = [(sql, param) for sql, param in conditions if param is not None]
    params = [param for sql, param in active for _ in range(sql.count("?"))]
    return " AND ".join(["true", *(sql for sql, _ in active)]), params


def _contains(text: str | None) -> str | None:
    return None if text is None else f"%{text}%"


def _companies_query(validated: ListCompaniesInput) -> tuple[str, list[Any]]:
    where, params = _where(
        [
            ("sector = ?", validated.sector),
            ("(company_id ILIKE ? OR name ILIKE ?)", _contains(validated.query)),
        ]
    )
    sql = (
        "SELECT company_id, name, sector, fiscal_year_end FROM companies "
        f"WHERE {where} ORDER BY company_id"
    )
    return sql, params


def _concepts_query(validated: ListFinancialConceptsInput) -> tuple[str, list[Any]]:
    where, params = _where(
        [
            ("company_id = ?", validated.company_id),
            ("(concept ILIKE ? OR label ILIKE ?)", _contains(validated.keyword)),
        ]
    )
    sql = (
        f"SELECT concept, label, units, taxonomy FROM concepts WHERE {where} "
        "ORDER BY concept, taxonomy"
    )
    return sql, params


def _facts_query(validated: GetFinancialFactInput) -> tuple[str, list[Any]]:
    where, params = _where(
        [
            ("company_id = ?", validated.company_id),
            ("concept = ?", validated.concept),
            ("fiscal_year = ?", validated.fiscal_year),
            ("fiscal_period = ?", validated.fiscal_period),
        ]
    )
    sql = (
        "SELECT fiscal_year, fiscal_period, period_start, period_end, concept, value, "
        f"unit, form, filed, accession FROM financial_facts WHERE {where} "
        "ORDER BY fiscal_year NULLS LAST, period_end, fiscal_period, filed, accession, "
        "period_start, unit, value"
    )
    return sql, params


def _filings_query(validated: SearchFilingsInput) -> tuple[str, list[Any]]:
    where, params = _where(
        [
            ("company_id = ?", validated.company_id),
            ("summary_text ILIKE ?", _contains(validated.keyword)),
            ("form_type = ?", validated.form_type),
            ("fiscal_year = ?", validated.fiscal_year),
        ]
    )
    sql = (
        "SELECT form_type, fiscal_year, fiscal_period, filed_date, report_date, "
        f"accession, primary_document, summary_text FROM filings WHERE {where} "
        "ORDER BY filed_date"
    )
    return sql, params


def list_companies(
    conn: duckdb.DuckDBPyConnection,
    sector: str | None = None,
    query: str | None = None,
) -> ToolResult:
    """List known companies, optionally filtered by sector or searched by name.

    Use this first to find a company's `company_id` (its ticker) — every
    other tool needs one. `query` matches part of a ticker or registered
    name, case-insensitively, e.g. a distinctive word of the company's name.
    `sector` is an exact match on the SEC industry description.
    """
    start = time.perf_counter()
    hint = "`sector` and `query`, if given, must be non-empty strings."
    validated = _validate(ListCompaniesInput, start, hint, sector=sector, query=query)
    if isinstance(validated, ToolResult):
        return validated

    sql, params = _companies_query(validated)
    return run_and_classify(
        start,
        conn,
        sql,
        params,
        empty_message=f"No companies found for sector={validated.sector!r}, "
        f"query={validated.query!r}. Try a shorter query or a different word of "
        "the name, or call with no filters to see everything available.",
        truncated_label="companies",
        truncated_hint="Narrow with `query` or `sector` to see the rest.",
        ok_noun="companies",
    )


def list_financial_concepts(
    conn: duckdb.DuckDBPyConnection, company_id: str, keyword: str | None = None
) -> ToolResult:
    """List which financial concepts (line items) exist for a company.

    Call this before `get_financial_fact` — concept names aren't guessable,
    and the same line item can have different names at different companies
    (e.g. `Revenues` vs `RevenueFromContractWithCustomerExcludingAssessedTax`).
    A real company reports hundreds of concepts, so pass `keyword` to match
    part of the concept name or its human-readable label, e.g. "revenue",
    "gross profit", "income tax", "inventory".
    """
    start = time.perf_counter()
    hint = "`company_id` must be a non-empty string."
    validated = _validate(
        ListFinancialConceptsInput, start, hint, company_id=company_id, keyword=keyword
    )
    if isinstance(validated, ToolResult):
        return validated
    if (company_error := check_company(start, conn, validated.company_id)) is not None:
        return company_error

    sql, params = _concepts_query(validated)
    matching = (
        f" matching {validated.keyword!r}. Try a shorter or different keyword."
        if validated.keyword is not None
        else "."
    )
    return run_and_classify(
        start,
        conn,
        sql,
        params,
        empty_message=f"No financial concepts recorded for {validated.company_id!r}"
        + matching,
        truncated_label="concepts",
        truncated_hint="Pass `keyword` to narrow the list.",
        ok_noun=f"concepts for {validated.company_id!r}",
    )


def get_financial_fact(
    conn: duckdb.DuckDBPyConnection,
    company_id: str,
    concept: str,
    fiscal_year: int | None = None,
    fiscal_period: Literal["FY", "Q1", "Q2", "Q3", "Q4"] | None = None,
) -> ToolResult:
    """Look up a financial concept's value for a company, as filed.

    `concept` must come from `list_financial_concepts` — don't guess a name.
    Omit `fiscal_year`/`fiscal_period` to get every recorded period, e.g. for
    a trend question. `fiscal_year`/`fiscal_period` follow the company's own
    fiscal calendar, which may not match the calendar year. Each row carries
    its period dates and the filing it came from (form, filed date,
    accession): cite those as evidence.

    Rows with a null `fiscal_period` are year-to-date totals (e.g. nine
    months), not quarters. Q4 is usually not reported on its own: derive it
    as the FY value minus the nine-month year-to-date value. Values are in
    the unit shown, unscaled (e.g. USD, not USD millions).
    """
    start = time.perf_counter()
    validated = _validate(
        GetFinancialFactInput,
        start,
        None,
        company_id=company_id,
        concept=concept,
        fiscal_year=fiscal_year,
        fiscal_period=fiscal_period,
    )
    if isinstance(validated, ToolResult):
        return validated
    if (company_error := check_company(start, conn, validated.company_id)) is not None:
        return company_error

    sql, params = _facts_query(validated)
    return run_and_classify(
        start,
        conn,
        sql,
        params,
        empty_message=f"No {validated.concept!r} fact for {validated.company_id!r} "
        "with the given filters. Try list_financial_concepts, or drop fiscal_year/"
        "fiscal_period to widen the search.",
        truncated_label="facts",
        truncated_hint="Narrow with fiscal_year or fiscal_period.",
        ok_noun=f"fact(s) for {validated.concept!r} on {validated.company_id!r}",
    )


def search_filings(
    conn: duckdb.DuckDBPyConnection,
    company_id: str,
    keyword: str | None = None,
    form_type: FormType | None = None,
    fiscal_year: int | None = None,
) -> ToolResult:
    """List a company's filings by keyword, form type, or fiscal year.

    Returns filing metadata only — form, dates, accession, document name and
    a short description (for an 8-K, its item numbers; item 2.02 is an
    earnings release). It can't read document text, so it can't answer
    questions that need guidance, narrative, or a specific exhibit. 8-Ks
    have no `fiscal_year`; find them with `form_type` and the filed dates.
    Omit every filter to list everything on file for the company.
    """
    start = time.perf_counter()
    validated = _validate(
        SearchFilingsInput,
        start,
        None,
        company_id=company_id,
        keyword=keyword,
        form_type=form_type,
        fiscal_year=fiscal_year,
    )
    if isinstance(validated, ToolResult):
        return validated
    if (company_error := check_company(start, conn, validated.company_id)) is not None:
        return company_error

    sql, params = _filings_query(validated)
    return run_and_classify(
        start,
        conn,
        sql,
        params,
        empty_message=f"No filings match for {validated.company_id!r} with the "
        "given filters. Try dropping keyword/form_type/fiscal_year to widen the "
        "search.",
        truncated_label="filings",
        truncated_hint="Narrow with keyword, form_type, or fiscal_year.",
        ok_noun=f"filing(s) for {validated.company_id!r}",
    )
