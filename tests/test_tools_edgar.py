"""The tools against a normalized SEC EDGAR snapshot, and the data-source switch.

The snapshot is built from a tiny, fictional raw SEC-shaped payload through
the real normalizer, so these tests cover the schema the tools actually get
in production, not just the fixture's copy of it.
"""

import json
from datetime import date
from pathlib import Path

import duckdb
import pytest

from recon.adapters.sec_edgar import EdgarConfig
from recon.adapters.sec_edgar_normalize import normalize_snapshot
from recon.tools import data_source, server

CIK = 1234567
TICKER = "EXWD"
TEN_K = ("0001-25-000001", "10-K", "2025-02-14", "2024-12-31")
EIGHT_K = ("0001-25-000002", "8-K", "2025-01-30", "2025-01-30")


def _entry(val: float, start: str, end: str) -> dict[str, object]:
    return {
        "start": start,
        "end": end,
        "val": val,
        "accn": TEN_K[0],
        "fy": 2024,
        "fp": "FY",
        "form": "10-K",
        "filed": TEN_K[2],
    }


def _build(tmp_path: Path) -> Path:
    raw = tmp_path / "raw"
    (raw / "submissions").mkdir(parents=True)
    (raw / "companyfacts").mkdir()
    filings = [TEN_K, EIGHT_K]
    main = {
        "name": "Example Widgets, Inc.",
        "sicDescription": "Widget Manufacturing",
        "fiscalYearEnd": "1231",
        "filings": {
            "recent": {
                "accessionNumber": [f[0] for f in filings],
                "form": [f[1] for f in filings],
                "filingDate": [f[2] for f in filings],
                "reportDate": [f[3] for f in filings],
                "primaryDocument": ["annual.htm", "release.htm"],
                "primaryDocDescription": ["10-K", "8-K"],
                "items": ["", "2.02,9.01"],
            }
        },
    }
    (raw / "submissions" / f"CIK{CIK:010d}.json").write_text(json.dumps(main))
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "label": "Revenues",
                    "description": "Total revenue.",
                    "units": {"USD": [_entry(130e6, "2024-01-01", "2024-12-31")]},
                },
                "GrossProfit": {
                    "label": "Gross Profit",
                    "description": "Revenue less cost of revenue.",
                    "units": {"USD": [_entry(52e6, "2024-01-01", "2024-12-31")]},
                },
            }
        }
    }
    (raw / "companyfacts" / f"CIK{CIK:010d}.json").write_text(json.dumps(facts))
    processed = tmp_path / "processed"
    config = EdgarConfig(date(2025, 4, 7), ("us-gaap",), ("10-K", "8-K"), 5.0)
    normalize_snapshot(raw, {TICKER: CIK}, config, processed / "20260928")
    return processed


@pytest.fixture
def edgar_conn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> duckdb.DuckDBPyConnection:
    monkeypatch.setenv(data_source.TOOL_DATA_ENV, "edgar")
    return data_source.open_tool_data(_build(tmp_path))


@pytest.mark.unit
def test_company_found_by_part_of_its_name(edgar_conn) -> None:
    result = server.list_companies(edgar_conn, query="widgets")
    assert result.data == [
        {
            "company_id": TICKER,
            "name": "Example Widgets, Inc.",
            "sector": "Widget Manufacturing",
            "fiscal_year_end": "12-31",
        }
    ]


@pytest.mark.unit
def test_concepts_searched_by_label(edgar_conn) -> None:
    result = server.list_financial_concepts(edgar_conn, TICKER, keyword="gross profit")
    assert result.data == [
        {
            "concept": "GrossProfit",
            "label": "Gross Profit",
            "units": "USD",
            "taxonomy": "us-gaap",
        }
    ]


@pytest.mark.unit
def test_fact_rows_carry_dates_as_strings_and_the_source_filing(edgar_conn) -> None:
    result = server.get_financial_fact(
        edgar_conn, TICKER, "Revenues", fiscal_year=2024, fiscal_period="FY"
    )
    assert result.status == "ok"
    assert result.data == [
        {
            "fiscal_year": 2024,
            "fiscal_period": "FY",
            "period_start": "2024-01-01",
            "period_end": "2024-12-31",
            "concept": "Revenues",
            "value": 130e6,
            "unit": "USD",
            "form": "10-K",
            "filed": "2025-02-14",
            "accession": TEN_K[0],
        }
    ]
    json.dumps(result.model_dump())  # what the MCP layer does with it


@pytest.mark.unit
def test_earnings_release_found_by_form_and_item(edgar_conn) -> None:
    result = server.search_filings(edgar_conn, TICKER, keyword="2.02", form_type="8-K")
    (row,) = result.data
    assert (row["accession"], row["filed_date"], row["fiscal_year"]) == (
        EIGHT_K[0],
        "2025-01-30",
        None,
    )
    assert row["summary_text"] == "8-K; items 2.02,9.01"


@pytest.mark.unit
def test_fixture_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(data_source.TOOL_DATA_ENV, "fixture")
    conn = data_source.open_tool_data()
    assert server.list_companies(conn, query="FIRM-001").row_count == 1


@pytest.mark.unit
def test_edgar_is_the_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(data_source.TOOL_DATA_ENV)
    conn = data_source.open_tool_data(_build(tmp_path))
    assert server.list_companies(conn).data[0]["company_id"] == TICKER


@pytest.mark.unit
def test_missing_edgar_cache_is_an_error_not_a_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(data_source.TOOL_DATA_ENV, "edgar")
    with pytest.raises(FileNotFoundError, match="edgar fetch"):
        data_source.open_tool_data(tmp_path / "empty")


@pytest.mark.unit
def test_unknown_source_names_the_valid_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(data_source.TOOL_DATA_ENV, "live")
    with pytest.raises(ValueError, match="'edgar'.*'fixture'"):
        data_source.open_tool_data()


@pytest.mark.unit
def test_snapshot_id_for_the_fixture_is_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(data_source.TOOL_DATA_ENV, "fixture")
    snapshot = data_source.tool_data_snapshot_id()
    assert snapshot.startswith("fixture-")
    assert data_source.tool_data_snapshot_id() == snapshot


@pytest.mark.unit
def test_snapshot_id_names_the_snapshot_open_tool_data_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(data_source.TOOL_DATA_ENV, "edgar")
    processed = _build(tmp_path)
    snapshot = data_source.tool_data_snapshot_id(processed)
    date_part, content_hash = snapshot.split("-")
    assert date_part == data_source.latest_snapshot(processed).name == "20260928"
    assert len(content_hash) == 12
    assert data_source.tool_data_snapshot_id(processed) == snapshot


@pytest.mark.unit
def test_same_day_snapshot_with_different_data_gets_a_new_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-day re-fetch overwrites the same directory, e.g. after a moved
    filed_cutoff drops facts. The date alone would call the runs comparable."""
    monkeypatch.setenv(data_source.TOOL_DATA_ENV, "edgar")
    processed = _build(tmp_path)
    before = data_source.tool_data_snapshot_id(processed)
    facts = processed / "20260928" / "financial_facts.parquet"
    path = str(facts).replace("'", "''")
    duckdb.connect().execute(
        f"COPY (SELECT * FROM read_parquet('{path}') LIMIT 0) "
        f"TO '{path}.new' (FORMAT parquet)"
    )
    Path(f"{facts}.new").replace(facts)
    after = data_source.tool_data_snapshot_id(processed)
    assert after.split("-")[0] == before.split("-")[0] == "20260928"
    assert after != before


@pytest.mark.unit
def test_snapshot_id_raises_like_open_tool_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(data_source.TOOL_DATA_ENV, "edgar")
    with pytest.raises(FileNotFoundError, match="edgar fetch"):
        data_source.tool_data_snapshot_id(tmp_path / "empty")
    monkeypatch.setenv(data_source.TOOL_DATA_ENV, "live")
    with pytest.raises(ValueError, match="'edgar'.*'fixture'"):
        data_source.tool_data_snapshot_id()
