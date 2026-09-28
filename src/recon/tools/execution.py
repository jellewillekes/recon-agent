"""How every tool in `server.py` runs its query and shapes its `ToolResult`.

Bounded execution (a per-call cursor, `TIMEOUT_S`), retry with backoff and
jitter, and a per-connection circuit breaker (`docs/adr/0009-tool-reliability-
and-run-budgets.md`), plus the shared mapping of a query's outcome onto the
five `ToolResult` statuses (`docs/contracts.md` section 3).

Tests monkeypatch `TIMEOUT_S` and the retry/breaker constants on this module,
where they are read.
"""

import concurrent.futures
import random
import time
import weakref
from datetime import date
from typing import Any, Literal

import duckdb

from recon.contracts import ToolResult

MAX_ROWS = 500
TIMEOUT_S = 30.0

# Retry: up to this many attempts total per call, exponential backoff + jitter
# between them - except a timeout (ToolTimeout), which is never retried; see
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


class ToolUnavailable(Exception):
    """Raised by `run_bounded`, turned into an `unavailable` result."""


class ToolTimeout(ToolUnavailable):
    """Raised by `_run_bounded_once` specifically for a `TIMEOUT_S` timeout.

    Kept distinct from a plain `ToolUnavailable` so `run_bounded` can skip
    retrying it: a query that already burned the full `TIMEOUT_S` waiting is
    a much stronger signal of a stuck source than a fast `duckdb.Error`, and
    retrying it up to `_MAX_ATTEMPTS` times would multiply, not shorten, the
    wait — up to `_MAX_ATTEMPTS * TIMEOUT_S` per call before the breaker even
    sees a failure, which can outrun a runtime's own wall-clock budget.
    """


class CircuitBreaker:
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
# A plain `dict[int, CircuitBreaker]` keyed by id(conn) would be unsafe here:
# CPython frees an object's memory as soon as its refcount hits zero, and a
# same-sized allocation right after can reuse that address - exactly the
# pattern of short-lived per-test connections. A `WeakKeyDictionary` instead
# drops the entry itself once `conn` is garbage-collected, so a reused id
# never resolves to a stale breaker, and the registry can't grow without
# bound either.
_BREAKERS: "weakref.WeakKeyDictionary[duckdb.DuckDBPyConnection, CircuitBreaker]" = (
    weakref.WeakKeyDictionary()
)


def _breaker_for(conn: duckdb.DuckDBPyConnection) -> CircuitBreaker:
    # Reads the module-level constants at call time, not as CircuitBreaker's
    # own default arguments (bound once at class-definition time) - so a test
    # monkeypatching _BREAKER_THRESHOLD/_BREAKER_COOLDOWN_S actually takes
    # effect for breakers created afterward.
    if conn not in _BREAKERS:
        _BREAKERS[conn] = CircuitBreaker(_BREAKER_THRESHOLD, _BREAKER_COOLDOWN_S)
    return _BREAKERS[conn]


def elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def failure(
    start: float,
    status: Literal["invalid_input", "unavailable", "empty"],
    message: str,
) -> ToolResult:
    """A `ToolResult` with no data: `invalid_input`, `unavailable` or `empty`."""
    return ToolResult(
        status=status,
        data=[],
        row_count=0,
        message=message,
        elapsed_ms=elapsed_ms(start),
    )


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
    DuckDB error or a TIMEOUT_S timeout into `ToolUnavailable`. One attempt -
    `run_bounded` is what adds retry and the circuit breaker around this.

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
        raise ToolUnavailable(
            f"Could not open a connection: {exc}. The data source may be "
            "temporarily unavailable — retrying is worth trying once."
        ) from exc

    future = _EXECUTOR.submit(_execute_and_fetch, cursor, sql, params)
    try:
        return future.result(timeout=TIMEOUT_S)
    except concurrent.futures.TimeoutError as exc:
        cursor.interrupt()
        raise ToolTimeout(
            f"Query exceeded the {TIMEOUT_S}s timeout and was cancelled. Narrow "
            "the request and retry."
        ) from exc
    except duckdb.Error as exc:
        raise ToolUnavailable(
            f"Query failed: {exc}. The data source may be temporarily unavailable — "
            "retrying is worth trying once."
        ) from exc


def run_bounded(
    conn: duckdb.DuckDBPyConnection, sql: str, params: list[Any]
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Retry `_run_bounded_once` with exponential backoff + jitter, behind a
    circuit breaker scoped to `conn`.

    An open breaker skips the query (and every retry) entirely and raises
    immediately - the whole point is to stop hammering a source that's
    already shown it's down. A call that exhausts its retries counts as one
    failure toward the breaker; three such calls in a row open it.

    A `ToolTimeout` is never retried - it already spent the full
    `TIMEOUT_S` once, so it counts as this call's failure immediately
    instead of burning `_MAX_ATTEMPTS` full waits in a row.
    """
    breaker = _breaker_for(conn)
    if breaker.is_open:
        raise ToolUnavailable(
            "The data source has failed repeatedly and the circuit breaker is "
            "open. Wait before retrying."
        )

    last_exc: ToolUnavailable | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            result = _run_bounded_once(conn, sql, params)
        except ToolTimeout:
            breaker.record_failure()
            raise
        except ToolUnavailable as exc:
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


def check_company(
    start: float, conn: duckdb.DuckDBPyConnection, company_id: str
) -> ToolResult | None:
    """Returns a `ToolResult` if `company_id` is unknown or the lookup
    itself failed, else `None` so the caller knows it can proceed."""
    try:
        _columns, rows = run_bounded(
            conn, "SELECT 1 FROM companies WHERE company_id = ?", [company_id]
        )
    except ToolUnavailable as exc:
        return failure(start, "unavailable", str(exc))
    if not rows:
        return failure(
            start,
            "invalid_input",
            f"Unknown company_id {company_id!r}. Call list_companies for valid ids.",
        )
    return None


def run_and_classify(
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
    """Shared shape for every tool in `server.py`: run a bounded query, then
    turn the result into `unavailable` / `empty` / `truncated` / `ok`. Input
    validation and the `invalid_input` cases stay in each tool, since those
    messages are specific to what that tool accepts.
    """
    try:
        columns, rows = run_bounded(conn, sql, params)
    except ToolUnavailable as exc:
        return failure(start, "unavailable", str(exc))

    if not rows:
        return failure(start, "empty", empty_message)

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
            elapsed_ms=elapsed_ms(start),
        )

    return ToolResult(
        status="ok",
        data=data,
        row_count=len(data),
        message=f"{len(data)} {ok_noun}.",
        elapsed_ms=elapsed_ms(start),
    )
