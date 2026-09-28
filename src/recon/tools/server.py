"""Read-only, DuckDB-backed research tools.

Every tool returns a `ToolResult` (`docs/contracts.md` section 3) and covers
all five statuses. Data comes from a normalized SEC EDGAR snapshot or the
synthetic fixture in `fixtures.py`, chosen by `data_source.py`. Both share
one schema, so the queries below serve either — see `docs/data-sources.md`.

Tool functions below take the DuckDB connection as their first argument and
are called directly in tests, with no MCP transport and no LLM involved.
`mcp_server.py` wires the same functions to an `MCPServer` for real stdio use.
"""

import concurrent.futures
import random
import time
import weakref
from datetime import date
from typing import Any, Literal

import duckdb
from pydantic import BaseModel, Field, ValidationError

from recon.contracts import ToolResult

MAX_ROWS = 500
TIMEOUT_S = 30.0

# Retry: up to this many attempts total per call, exponential backoff + jitter
# between them - except a timeout (_ToolTimeout), which is never retried; see
# its docstring. Circuit breaker: this many *calls* (not retry attempts
# within one call) failing consecutively opens the breaker for that
# connection - further calls short-circuit straight to `unavailable` with no
# query attempt at all, until the cooldown elapses and lets one probe call
# through.
_MAX_ATTEMPTS = 3
_BASE_DELAY_S = 0.01
_BREAKER_THRESHOLD = 3
_BREAKER_COOLDOWN_S = 30.0

_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4)


class _ToolUnavailable(Exception):
    """Raised by `_run_bounded`, caught by each tool to build an `unavailable` result."""


class _ToolTimeout(_ToolUnavailable):
    """Raised by `_run_bounded_once` specifically for a `TIMEOUT_S` timeout.

    Kept distinct from a plain `_ToolUnavailable` so `_run_bounded` can skip
    retrying it: a query that already burned the full `TIMEOUT_S` waiting is
    a much stronger signal of a stuck source than a fast `duckdb.Error`, and
    retrying it up to `_MAX_ATTEMPTS` times would multiply, not shorten, the
    wait — up to `_MAX_ATTEMPTS * TIMEOUT_S` per call before the breaker even
    sees a failure, which can outrun a runtime's own wall-clock budget.
    """


class _CircuitBreaker:
    """Consecutive-failure counter for one data source, with a cooldown so it
    can recover on its own.

    `is_open` once `_BREAKER_THRESHOLD` calls in a row have failed - but only
    for `_BREAKER_COOLDOWN_S` after the most recent failure. Once that
    elapses, `is_open` goes back to `False` for exactly one call: a probe.
    If it succeeds, `record_success` resets the breaker fully closed; if it
    fails, `record_failure` re-opens it and restarts the cooldown. Without
    this, an open breaker would never let a single call through again to
    find out the source recovered.
    """

    def __init__(
        self,
        threshold: int = _BREAKER_THRESHOLD,
        cooldown_s: float = _BREAKER_COOLDOWN_S,
    ) -> None:
        self._threshold = threshold
        self._cooldown_s = cooldown_s
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._consecutive_failures < self._threshold:
            return False
        assert self._opened_at is not None
        return (time.monotonic() - self._opened_at) < self._cooldown_s

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._threshold:
            self._opened_at = time.monotonic()


# One breaker per DuckDB connection, keyed by identity rather than a single
# global: production holds one long-lived `conn` for the server process's
# lifetime, while each test gets its own fresh `duckdb.connect(":memory:")` -
# keying by the connection object itself gives every test an independent
# breaker automatically, with no explicit reset needed between them.
#
# A plain `dict[int, _CircuitBreaker]` keyed by id(conn) would be unsafe here:
# CPython frees an object's memory as soon as its refcount hits zero, and a
# same-sized allocation right after can reuse that address - exactly the
# pattern of short-lived per-test connections. A `WeakKeyDictionary` instead
# drops the entry itself once `conn` is garbage-collected, so a reused id
# never resolves to a stale breaker, and the registry can't grow without
# bound either.
_BREAKERS: "weakref.WeakKeyDictionary[duckdb.DuckDBPyConnection, _CircuitBreaker]" = (
    weakref.WeakKeyDictionary()
)


def _breaker_for(conn: duckdb.DuckDBPyConnection) -> _CircuitBreaker:
    # Reads the module-level constants at call time, not as _CircuitBreaker's
    # own default arguments (bound once at class-definition time) - so a test
    # monkeypatching _BREAKER_THRESHOLD/_BREAKER_COOLDOWN_S actually takes
    # effect for breakers created afterward.
    if conn not in _BREAKERS:
        _BREAKERS[conn] = _CircuitBreaker(_BREAKER_THRESHOLD, _BREAKER_COOLDOWN_S)
    return _BREAKERS[conn]


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _execute_and_fetch(
    cursor: duckdb.DuckDBPyConnection, sql: str, params: list[Any]
) -> tuple[list[str], list[tuple[Any, ...]]]:
    result = cursor.execute(sql, params)
    columns = [d[0] for d in result.description]
    return columns, result.fetchall()


def _run_bounded_once(
    conn: duckdb.DuckDBPyConnection, sql: str, params: list[Any]
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Run `sql` on a fresh cursor duplicated from `conn`, converting a
    DuckDB error or a TIMEOUT_S timeout into `_ToolUnavailable`. One attempt -
    `_run_bounded` is what adds retry and the circuit breaker around this.

    Each call gets its own cursor rather than running on the shared `conn`
    directly: `docs/contracts.md` section 2 rules out concurrent access to
    one DuckDB connection, and a call that times out keeps running in its
    worker thread after we give up waiting on it. `cursor.interrupt()` on
    timeout cancels that abandoned query for real, instead of leaving it to
    run to completion and permanently hold a slot in the executor's pool.
    """
    try:
        cursor = conn.cursor()
    except duckdb.Error as exc:
        raise _ToolUnavailable(
            f"Could not open a connection: {exc}. The data source may be "
            "temporarily unavailable — retrying is worth trying once."
        ) from exc

    future = _EXECUTOR.submit(_execute_and_fetch, cursor, sql, params)
    try:
        return future.result(timeout=TIMEOUT_S)
    except concurrent.futures.TimeoutError as exc:
        cursor.interrupt()
        raise _ToolTimeout(
            f"Query exceeded the {TIMEOUT_S}s timeout and was cancelled. Narrow "
            "the request and retry."
        ) from exc
    except duckdb.Error as exc:
        raise _ToolUnavailable(
            f"Query failed: {exc}. The data source may be temporarily unavailable — "
            "retrying is worth trying once."
        ) from exc


def _run_bounded(
    conn: duckdb.DuckDBPyConnection, sql: str, params: list[Any]
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Retry `_run_bounded_once` with exponential backoff + jitter, behind a
    circuit breaker scoped to `conn`.

    An open breaker skips the query (and every retry) entirely and raises
    immediately - the whole point is to stop hammering a source that's
    already shown it's down. A call that exhausts its retries counts as one
    failure toward the breaker; three such calls in a row open it.

    A `_ToolTimeout` is never retried - it already spent the full
    `TIMEOUT_S` once, so it counts as this call's failure immediately
    instead of burning `_MAX_ATTEMPTS` full waits in a row.
    """
    breaker = _breaker_for(conn)
    if breaker.is_open:
        raise _ToolUnavailable(
            "The data source has failed repeatedly and the circuit breaker is "
            "open. Wait before retrying."
        )

    last_exc: _ToolUnavailable | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            result = _run_bounded_once(conn, sql, params)
        except _ToolTimeout:
            breaker.record_failure()
            raise
        except _ToolUnavailable as exc:
            last_exc = exc
            if attempt < _MAX_ATTEMPTS - 1:
                delay = _BASE_DELAY_S * (2**attempt) + random.uniform(0, _BASE_DELAY_S)
                time.sleep(delay)
            continue
        breaker.record_success()
        return result

    breaker.record_failure()
    assert last_exc is not None  # loop always sets it before falling through
    raise last_exc


def _rows_to_dicts(
    columns: list[str], rows: list[tuple[Any, ...]]
) -> list[dict[str, Any]]:
    # Dates as ISO strings: the MCP layer serializes this to JSON, and the
    # EDGAR tables carry real DATE columns where the fixture has strings.
    return [
        {
            column: value.isoformat() if isinstance(value, date) else value
            for column, value in zip(columns, row, strict=True)
        }
        for row in rows
    ]


def _company_exists(conn: duckdb.DuckDBPyConnection, company_id: str) -> bool:
    _columns, rows = _run_bounded(
        conn, "SELECT 1 FROM companies WHERE company_id = ?", [company_id]
    )
    return len(rows) > 0


def _check_company(
    start: float, conn: duckdb.DuckDBPyConnection, company_id: str
) -> ToolResult | None:
    """Returns a `ToolResult` if `company_id` is unknown or the lookup
    itself failed, else `None` so the caller knows it can proceed."""
    try:
        exists = _company_exists(conn, company_id)
    except _ToolUnavailable as exc:
        return ToolResult(
            status="unavailable",
            data=[],
            row_count=0,
            message=str(exc),
            elapsed_ms=_elapsed_ms(start),
        )
    if not exists:
        return ToolResult(
            status="invalid_input",
            data=[],
            row_count=0,
            message=f"Unknown company_id {company_id!r}. Call list_companies for "
            "valid ids.",
            elapsed_ms=_elapsed_ms(start),
        )
    return None


def _run_and_classify(
    start: float,
    conn: duckdb.DuckDBPyConnection,
    sql: str,
    params: list[Any],
    *,
    empty_message: str,
    truncated_label: str,
    truncated_hint: str,
    ok_noun: str,
) -> ToolResult:
    """Shared shape for every tool below: run a bounded query, then turn the
    result into `unavailable` / `empty` / `truncated` / `ok`. Input
    validation and the `invalid_input` cases stay in each tool, since those
    messages are specific to what that tool accepts.
    """
    try:
        columns, rows = _run_bounded(conn, sql, params)
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
            message=empty_message,
            elapsed_ms=_elapsed_ms(start),
        )

    data = _rows_to_dicts(columns, rows)
    if len(data) > MAX_ROWS:
        message = f"{len(data)} {truncated_label} match, showing the first {MAX_ROWS}."
        if truncated_hint:
            message = f"{message} {truncated_hint}"
        return ToolResult(
            status="truncated",
            data=data[:MAX_ROWS],
            row_count=len(data),
            message=message,
            elapsed_ms=_elapsed_ms(start),
        )

    return ToolResult(
        status="ok",
        data=data,
        row_count=len(data),
        message=f"{len(data)} {ok_noun}.",
        elapsed_ms=_elapsed_ms(start),
    )


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
    try:
        validated = ListCompaniesInput(sector=sector, query=query)
    except ValidationError as exc:
        return ToolResult(
            status="invalid_input",
            data=[],
            row_count=0,
            message=f"Invalid input: {exc.errors()[0]['msg']}. `sector` and `query`, "
            "if given, must be non-empty strings.",
            elapsed_ms=_elapsed_ms(start),
        )

    conditions = ["true"]
    params: list[Any] = []
    if validated.sector is not None:
        conditions.append("sector = ?")
        params.append(validated.sector)
    if validated.query is not None:
        conditions.append("(company_id ILIKE ? OR name ILIKE ?)")
        params.extend([f"%{validated.query}%"] * 2)

    return _run_and_classify(
        start,
        conn,
        "SELECT company_id, name, sector, fiscal_year_end FROM companies "
        f"WHERE {' AND '.join(conditions)} ORDER BY company_id",
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
    try:
        validated = ListFinancialConceptsInput(company_id=company_id, keyword=keyword)
    except ValidationError as exc:
        return ToolResult(
            status="invalid_input",
            data=[],
            row_count=0,
            message=f"Invalid input: {exc.errors()[0]['msg']}. `company_id` must be a "
            "non-empty string.",
            elapsed_ms=_elapsed_ms(start),
        )

    if (company_error := _check_company(start, conn, validated.company_id)) is not None:
        return company_error

    conditions = ["company_id = ?"]
    params: list[Any] = [validated.company_id]
    if validated.keyword is not None:
        conditions.append("(concept ILIKE ? OR label ILIKE ?)")
        params.extend([f"%{validated.keyword}%"] * 2)

    return _run_and_classify(
        start,
        conn,
        "SELECT concept, label, units, taxonomy FROM concepts "
        f"WHERE {' AND '.join(conditions)} ORDER BY concept, taxonomy",
        params,
        empty_message=f"No financial concepts recorded for {validated.company_id!r}"
        + (
            f" matching {validated.keyword!r}. Try a shorter or different keyword."
            if validated.keyword is not None
            else "."
        ),
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

    if (company_error := _check_company(start, conn, validated.company_id)) is not None:
        return company_error

    conditions = ["company_id = ?", "concept = ?"]
    params: list[Any] = [validated.company_id, validated.concept]
    if validated.fiscal_year is not None:
        conditions.append("fiscal_year = ?")
        params.append(validated.fiscal_year)
    if validated.fiscal_period is not None:
        conditions.append("fiscal_period = ?")
        params.append(validated.fiscal_period)

    return _run_and_classify(
        start,
        conn,
        "SELECT fiscal_year, fiscal_period, period_start, period_end, concept, value, "
        "unit, form, filed, accession FROM financial_facts "
        f"WHERE {' AND '.join(conditions)} "
        "ORDER BY fiscal_year NULLS LAST, period_end, fiscal_period",
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

    if (company_error := _check_company(start, conn, validated.company_id)) is not None:
        return company_error

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

    return _run_and_classify(
        start,
        conn,
        "SELECT form_type, fiscal_year, fiscal_period, filed_date, report_date, "
        "accession, primary_document, summary_text "
        f"FROM filings WHERE {' AND '.join(conditions)} ORDER BY filed_date",
        params,
        empty_message=f"No filings match for {validated.company_id!r} with the "
        "given filters. Try dropping keyword/form_type/fiscal_year to widen the "
        "search.",
        truncated_label="filings",
        truncated_hint="Narrow with keyword, form_type, or fiscal_year.",
        ok_noun=f"filing(s) for {validated.company_id!r}",
    )
