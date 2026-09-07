"""Contract tests for the Step 3 MCP tools.

Calls the tool functions directly against a seeded in-memory DuckDB
connection — no MCP transport, no LLM, no network. Covers every
`ToolResult.status` value from `docs/contracts.md` section 3 for each tool.
"""

import time

import duckdb
import pytest

from recon.tools import fixtures, server


@pytest.fixture
def conn() -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(":memory:")
    fixtures.seed(connection)
    return connection


class _SlowConnection:
    """Wraps a real connection so a query takes longer than TIMEOUT_S.

    Mimics the bit of the DuckDB connection API `_run_bounded` relies on:
    `.cursor()` returns something `execute`-able, and `.interrupt()` exists
    so the timeout path's cancellation call doesn't blow up on this double.
    """

    def __init__(self, real: duckdb.DuckDBPyConnection, delay_s: float) -> None:
        self._real = real
        self._delay_s = delay_s

    def cursor(self) -> "_SlowConnection":
        return self

    def execute(self, sql: str, params: list[object]) -> duckdb.DuckDBPyConnection:
        time.sleep(self._delay_s)
        return self._real.execute(sql, params)

    def interrupt(self) -> None:
        pass


# --- list_companies ---------------------------------------------------------


@pytest.mark.unit
def test_list_companies_ok(conn: duckdb.DuckDBPyConnection) -> None:
    result = server.list_companies(conn, sector="Industrials")
    assert result.status == "ok"
    assert result.row_count == 2
    assert {row["company_id"] for row in result.data} == {"FIRM-001", "FIRM-004"}


@pytest.mark.unit
def test_list_companies_empty(conn: duckdb.DuckDBPyConnection) -> None:
    result = server.list_companies(conn, sector="Nonexistent Sector")
    assert result.status == "empty"
    assert result.data == []
    assert result.row_count == 0


@pytest.mark.unit
def test_list_companies_invalid_input(conn: duckdb.DuckDBPyConnection) -> None:
    result = server.list_companies(conn, sector="")
    assert result.status == "invalid_input"
    assert "sector" in result.message


@pytest.mark.unit
def test_list_companies_truncated(conn: duckdb.DuckDBPyConnection) -> None:
    result = server.list_companies(conn, sector=None)
    assert result.status == "truncated"
    assert len(result.data) == server.MAX_ROWS
    assert result.row_count > server.MAX_ROWS


@pytest.mark.unit
def test_list_companies_unavailable(conn: duckdb.DuckDBPyConnection) -> None:
    conn.close()
    result = server.list_companies(conn)
    assert result.status == "unavailable"
    assert result.data == []


# --- list_financial_concepts -------------------------------------------------


@pytest.mark.unit
def test_list_financial_concepts_ok(conn: duckdb.DuckDBPyConnection) -> None:
    result = server.list_financial_concepts(conn, "FIRM-001")
    assert result.status == "ok"
    assert result.row_count == 4
    assert {row["concept"] for row in result.data} == {
        "revenue",
        "net_income",
        "gross_margin_pct",
        "operating_expenses",
    }


@pytest.mark.unit
def test_list_financial_concepts_empty(conn: duckdb.DuckDBPyConnection) -> None:
    result = server.list_financial_concepts(conn, "FIRM-009")
    assert result.status == "empty"
    assert result.data == []


@pytest.mark.unit
def test_list_financial_concepts_invalid_input_unknown_company(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    result = server.list_financial_concepts(conn, "NOPE-999")
    assert result.status == "invalid_input"
    assert "NOPE-999" in result.message


@pytest.mark.unit
def test_list_financial_concepts_invalid_input_blank_company(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    result = server.list_financial_concepts(conn, "")
    assert result.status == "invalid_input"


@pytest.mark.unit
def test_list_financial_concepts_truncated(conn: duckdb.DuckDBPyConnection) -> None:
    result = server.list_financial_concepts(conn, "FIRM-003")
    assert result.status == "truncated"
    assert len(result.data) == server.MAX_ROWS
    assert result.row_count > server.MAX_ROWS


@pytest.mark.unit
def test_list_financial_concepts_unavailable(conn: duckdb.DuckDBPyConnection) -> None:
    conn.close()
    result = server.list_financial_concepts(conn, "FIRM-001")
    assert result.status == "unavailable"


# --- get_financial_fact -------------------------------------------------------


@pytest.mark.unit
def test_get_financial_fact_ok(conn: duckdb.DuckDBPyConnection) -> None:
    result = server.get_financial_fact(
        conn, "FIRM-001", "revenue", fiscal_year=2024, fiscal_period="Q1"
    )
    assert result.status == "ok"
    assert result.row_count == 1
    assert result.data[0]["concept"] == "revenue"


@pytest.mark.unit
def test_get_financial_fact_empty(conn: duckdb.DuckDBPyConnection) -> None:
    result = server.get_financial_fact(conn, "FIRM-001", "revenue", fiscal_year=1999)
    assert result.status == "empty"
    assert result.data == []


@pytest.mark.unit
def test_get_financial_fact_invalid_input_unknown_company(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    result = server.get_financial_fact(conn, "NOPE-999", "revenue")
    assert result.status == "invalid_input"


@pytest.mark.unit
def test_get_financial_fact_invalid_input_bad_fiscal_year(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    result = server.get_financial_fact(conn, "FIRM-001", "revenue", fiscal_year=1)
    assert result.status == "invalid_input"
    assert "fiscal_year" in result.message


@pytest.mark.unit
def test_get_financial_fact_truncated(conn: duckdb.DuckDBPyConnection) -> None:
    result = server.get_financial_fact(conn, "FIRM-004", "bulk_yearly_revenue")
    assert result.status == "truncated"
    assert len(result.data) == server.MAX_ROWS
    assert result.row_count > server.MAX_ROWS


@pytest.mark.unit
def test_get_financial_fact_unavailable(conn: duckdb.DuckDBPyConnection) -> None:
    conn.close()
    result = server.get_financial_fact(conn, "FIRM-001", "revenue")
    assert result.status == "unavailable"


# --- search_filings ------------------------------------------------------------


@pytest.mark.unit
def test_search_filings_ok(conn: duckdb.DuckDBPyConnection) -> None:
    result = server.search_filings(conn, "FIRM-003", keyword="capacity expansion")
    assert result.status == "ok"
    assert result.row_count == 2


@pytest.mark.unit
def test_search_filings_empty(conn: duckdb.DuckDBPyConnection) -> None:
    result = server.search_filings(conn, "FIRM-009")
    assert result.status == "empty"
    assert result.data == []


@pytest.mark.unit
def test_search_filings_invalid_input_unknown_company(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    result = server.search_filings(conn, "NOPE-999")
    assert result.status == "invalid_input"


@pytest.mark.unit
def test_search_filings_invalid_input_bad_form_type(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    result = server.search_filings(
        conn,
        "FIRM-001",
        form_type="not-a-form",  # type: ignore[arg-type]
    )
    assert result.status == "invalid_input"
    assert "form_type" in result.message


@pytest.mark.unit
def test_search_filings_truncated(conn: duckdb.DuckDBPyConnection) -> None:
    result = server.search_filings(conn, "FIRM-002")
    assert result.status == "truncated"
    assert len(result.data) == server.MAX_ROWS
    assert result.row_count > server.MAX_ROWS


@pytest.mark.unit
def test_search_filings_unavailable(conn: duckdb.DuckDBPyConnection) -> None:
    conn.close()
    result = server.search_filings(conn, "FIRM-001")
    assert result.status == "unavailable"


# --- TIMEOUT_S enforcement (mechanism-level, not per tool) -------------------


@pytest.mark.unit
def test_run_bounded_times_out_as_unavailable(
    conn: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    slow = _SlowConnection(conn, delay_s=0.2)
    monkeypatch.setattr(server, "TIMEOUT_S", 0.01)

    result = server.list_companies(slow, sector="Industrials")  # type: ignore[arg-type]

    assert result.status == "unavailable"
    assert "timeout" in result.message
