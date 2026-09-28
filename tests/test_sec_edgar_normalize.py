"""Tests for `adapters/sec_edgar_normalize.py` against a synthetic snapshot.

The company is fictional and every number made up. The snapshot is shaped
like SEC's real JSON so each derivation rule in docs/adr/0015 has a row to
check: comparative-year dedup, restatement, the cutoff, Q4 vs full year,
year-to-date periods, instants, and form filtering.
"""

import json
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pytest

from recon.adapters import sec_edgar_normalize as normalize
from recon.adapters import sec_edgar_store as store
from recon.adapters.sec_edgar import EdgarConfig

CIK = 1234567
TICKER = "EXWD"

# accession: (form, filed, report date)
A0 = ("0001-23-000001", "10-K", "2023-02-15", "2022-12-31")  # older page
A1 = ("0001-24-000001", "10-K", "2024-02-15", "2023-12-31")
A2 = ("0001-25-000001", "10-K", "2025-02-14", "2024-12-31")
A3 = ("0001-24-000002", "10-Q", "2024-11-01", "2024-09-30")
A4 = ("0001-25-000002", "10-Q", "2025-05-01", "2025-03-31")  # after cutoff
A5 = ("0001-24-000003", "4", "2024-03-01", "")  # form not kept
A6 = ("0001-25-000003", "8-K", "2025-01-30", "2025-01-30")

CONFIG = EdgarConfig(
    filed_cutoff=date(2025, 4, 7),
    taxonomies=("us-gaap", "dei"),
    forms=("10-K", "10-Q", "8-K"),
    max_requests_per_second=5.0,
)


def _arrays(filings: list[tuple[str, str, str, str]]) -> dict[str, list[str]]:
    return {
        "accessionNumber": [f[0] for f in filings],
        "form": [f[1] for f in filings],
        "filingDate": [f[2] for f in filings],
        "reportDate": [f[3] for f in filings],
        "primaryDocument": [f"doc{i}.htm" for i in range(len(filings))],
        "primaryDocDescription": ["" for _ in filings],
        "items": ["2.02,9.01" if f[1] == "8-K" else "" for f in filings],
    }


def _entry(
    filing: tuple[str, str, str, str],
    val: float,
    end: str,
    fy: int,
    fp: str,
    start: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "end": end,
        "val": val,
        "accn": filing[0],
        "fy": fy,
        "fp": fp,
        "form": filing[1],
        "filed": filing[2],
    }
    if start is not None:
        entry["start"] = start
    return entry


REVENUE = [
    _entry(A0, 100, "2022-12-31", 2022, "FY", "2022-01-01"),
    _entry(A1, 100, "2022-12-31", 2023, "FY", "2022-01-01"),  # comparative
    _entry(A1, 110, "2023-12-31", 2023, "FY", "2023-01-01"),
    _entry(A2, 112, "2023-12-31", 2024, "FY", "2023-01-01"),  # restated
    _entry(A2, 130, "2024-12-31", 2024, "FY", "2024-01-01"),
    _entry(A2, 35, "2024-12-31", 2024, "FY", "2024-10-01"),  # Q4 in a 10-K
    _entry(A3, 33, "2024-09-30", 2024, "Q3", "2024-07-01"),
    _entry(A3, 95, "2024-09-30", 2024, "Q3", "2024-01-01"),  # nine-month YTD
    _entry(A4, 40, "2025-03-31", 2025, "Q1", "2025-01-01"),  # after cutoff
]


def _write_snapshot(root: Path, *, with_facts: bool = True) -> Path:
    snapshot = root / "raw"
    (snapshot / "submissions").mkdir(parents=True)
    page_name = f"CIK{CIK:010d}-submissions-001.json"
    main = {
        "name": "Example Widgets, Inc.",
        "sicDescription": "Widget Manufacturing",
        "fiscalYearEnd": "1231",
        "filings": {
            "recent": _arrays([A1, A2, A3, A4, A5, A6]),
            "files": [{"name": page_name}],
        },
    }
    (snapshot / "submissions" / f"CIK{CIK:010d}.json").write_text(json.dumps(main))
    (snapshot / "submissions" / page_name).write_text(json.dumps(_arrays([A0])))
    if with_facts:
        facts = {
            "facts": {
                "us-gaap": {
                    "Revenues": {
                        "label": "Revenues",
                        "description": "Total revenue.",
                        "units": {"USD": REVENUE},
                    },
                    "Assets": {
                        "label": "Assets",
                        "description": "Total assets.",
                        "units": {"USD": [_entry(A2, 500, "2024-12-31", 2024, "FY")]},
                    },
                },
                "dei": {
                    "EntityCommonStockSharesOutstanding": {
                        "label": "Shares outstanding",
                        "description": "Cover page share count.",
                        "units": {"shares": [_entry(A2, 9, "2025-02-01", 2024, "FY")]},
                    }
                },
                "ifrs-full": {
                    "Revenue": {
                        "label": "Revenue",
                        "units": {"USD": [_entry(A2, 1, "2024-12-31", 2024, "FY")]},
                    }
                },
            }
        }
        (snapshot / "companyfacts").mkdir()
        (snapshot / "companyfacts" / f"CIK{CIK:010d}.json").write_text(
            json.dumps(facts)
        )
    return snapshot


@pytest.fixture
def built(tmp_path: Path) -> tuple[Path, dict[str, int]]:
    out = tmp_path / "processed" / "20260928"
    counts = normalize.normalize_snapshot(
        _write_snapshot(tmp_path), {TICKER: CIK}, CONFIG, out
    )
    return out, counts


def _query(out: Path, table: str, where: str = "true") -> list[dict[str, Any]]:
    result = duckdb.execute(
        f"SELECT * FROM read_parquet('{out}/{table}.parquet') WHERE {where} "
        "ORDER BY ALL"
    )
    columns = [d[0] for d in result.description]
    return [dict(zip(columns, row, strict=True)) for row in result.fetchall()]


def _revenue(out: Path, start: str, end: str) -> dict[str, Any]:
    rows = _query(
        out,
        "financial_facts",
        f"concept = 'Revenues' AND period_start = DATE '{start}' "
        f"AND period_end = DATE '{end}'",
    )
    assert len(rows) == 1
    return rows[0]


@pytest.mark.unit
def test_comparative_year_keeps_its_own_fiscal_labels(built) -> None:
    out, _ = built
    row = _revenue(out, "2022-01-01", "2022-12-31")
    assert (row["value"], row["fiscal_year"], row["fiscal_period"]) == (100, 2022, "FY")


@pytest.mark.unit
def test_restatement_takes_latest_value_but_original_period(built) -> None:
    out, _ = built
    row = _revenue(out, "2023-01-01", "2023-12-31")
    assert row["value"] == 112
    assert row["accession"] == A2[0]
    assert (row["fiscal_year"], row["fiscal_period"]) == (2023, "FY")


@pytest.mark.unit
def test_quarter_reported_in_a_10k_is_q4(built) -> None:
    out, _ = built
    row = _revenue(out, "2024-10-01", "2024-12-31")
    assert (row["fiscal_period"], row["duration_days"]) == ("Q4", 91)


@pytest.mark.unit
def test_quarter_in_a_10q_keeps_its_quarter(built) -> None:
    out, _ = built
    assert _revenue(out, "2024-07-01", "2024-09-30")["fiscal_period"] == "Q3"


@pytest.mark.unit
def test_year_to_date_has_no_fiscal_period(built) -> None:
    out, _ = built
    row = _revenue(out, "2024-01-01", "2024-09-30")
    assert (row["value"], row["fiscal_year"], row["fiscal_period"]) == (95, 2024, None)


@pytest.mark.unit
def test_facts_filed_after_cutoff_are_dropped(built) -> None:
    out, _ = built
    rows = _query(out, "financial_facts", "period_end > DATE '2025-01-01'")
    assert [r["concept"] for r in rows] == ["EntityCommonStockSharesOutstanding"]


@pytest.mark.unit
def test_instant_fact(built) -> None:
    out, _ = built
    (row,) = _query(out, "financial_facts", "concept = 'Assets'")
    assert row["period_type"] == "instant"
    assert row["period_start"] is None and row["duration_days"] is None
    assert (row["fiscal_year"], row["fiscal_period"]) == (2024, "FY")


@pytest.mark.unit
def test_fact_that_was_never_a_current_period_has_no_fiscal_labels(built) -> None:
    # The cover-page share count is dated after the period end, so no filing
    # reported it as its current period.
    out, _ = built
    (row,) = _query(out, "financial_facts", "taxonomy = 'dei'")
    assert (row["value"], row["unit"]) == (9, "shares")
    assert (row["fiscal_year"], row["fiscal_period"]) == (None, None)


@pytest.mark.unit
def test_taxonomies_outside_config_are_skipped(built) -> None:
    out, _ = built
    assert _query(out, "financial_facts", "taxonomy = 'ifrs-full'") == []
    assert {r["concept"] for r in _query(out, "concepts")} == {
        "Assets",
        "EntityCommonStockSharesOutstanding",
        "Revenues",
    }


@pytest.mark.unit
def test_concepts_carry_labels_and_units(built) -> None:
    out, _ = built
    (row,) = _query(out, "concepts", "concept = 'Revenues'")
    assert (row["label"], row["units"], row["company_id"]) == (
        "Revenues",
        "USD",
        TICKER,
    )


@pytest.mark.unit
def test_company_row(built) -> None:
    out, _ = built
    (row,) = _query(out, "companies")
    assert row == {
        "company_id": TICKER,
        "cik": CIK,
        "name": "Example Widgets, Inc.",
        "sector": "Widget Manufacturing",
        "fiscal_year_end": "12-31",
    }


@pytest.mark.unit
def test_filings_are_filtered_by_form_and_cutoff(built) -> None:
    out, _ = built
    accessions = {r["accession"] for r in _query(out, "filings")}
    assert accessions == {A0[0], A1[0], A2[0], A3[0], A6[0]}


@pytest.mark.unit
def test_filing_rows_carry_period_and_items(built) -> None:
    out, _ = built
    (annual,) = _query(out, "filings", f"accession = '{A2[0]}'")
    assert (annual["fiscal_year"], annual["fiscal_period"]) == (2024, "FY")
    assert annual["report_date"] == date(2024, 12, 31)
    (current,) = _query(out, "filings", f"accession = '{A6[0]}'")
    assert current["items"] == "2.02,9.01"
    assert current["summary_text"] == "8-K; items 2.02,9.01"


@pytest.mark.unit
def test_counts_and_manifest(built) -> None:
    out, counts = built
    assert counts["companies"] == 1
    assert counts["financial_facts"] == len(_query(out, "financial_facts"))
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["filed_cutoff"] == "2025-04-07"
    assert manifest["row_counts"] == counts


@pytest.mark.unit
def test_company_without_xbrl_facts_still_gets_a_row(tmp_path: Path) -> None:
    out = tmp_path / "out"
    snapshot = _write_snapshot(tmp_path, with_facts=False)
    counts = normalize.normalize_snapshot(snapshot, {TICKER: CIK}, CONFIG, out)
    assert (counts["companies"], counts["financial_facts"]) == (1, 0)
    assert store.company_stats(out) == [(TICKER, 0, 5)]


@pytest.mark.unit
def test_latest_snapshot(tmp_path: Path) -> None:
    for name in ("20260101", "20260928", "20260301"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "manifest.json").write_text("{}")
    (tmp_path / "20261231").mkdir()  # no manifest: an interrupted build
    assert store.latest_snapshot(tmp_path).name == "20260928"


@pytest.mark.unit
def test_latest_snapshot_missing_says_what_to_run(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="edgar fetch"):
        store.latest_snapshot(tmp_path)


@pytest.mark.unit
def test_parquet_writer_keeps_quotes_commas_and_newlines(tmp_path: Path) -> None:
    # Real filing descriptions contain these, e.g. `8-K OF THE COMPANY ("EXWD")`.
    # DuckDB's CSV sniffer once guessed the escape character wrong on them.
    tricky = 'FORM 8-K OF THE COMPANY ("EXWD"), items 9\nsecond line'
    rows = [("EXWD", tricky, 20241231)] + [("PAD", "plain", i) for i in range(50)]
    dest = tmp_path / "t.parquet"
    count = store.write_parquet(
        rows, {"company_id": "VARCHAR", "text": "VARCHAR", "n": "BIGINT"}, dest
    )
    assert count == 51
    row = duckdb.execute(
        f"SELECT text FROM read_parquet('{dest}') WHERE company_id = 'EXWD'"
    ).fetchone()
    assert row is not None and row[0] == tricky


@pytest.mark.unit
def test_failed_rebuild_keeps_the_previous_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "processed" / "20260928"
    snapshot = _write_snapshot(tmp_path)
    normalize.normalize_snapshot(snapshot, {TICKER: CIK}, CONFIG, out)
    before = {p.name: p.read_bytes() for p in out.iterdir()}

    def broken_filing_rows(*args: object, **kwargs: object):
        raise RuntimeError("malformed filing")

    monkeypatch.setattr(normalize, "_filing_rows", broken_filing_rows)
    with pytest.raises(RuntimeError, match="malformed filing"):
        normalize.normalize_snapshot(snapshot, {TICKER: CIK}, CONFIG, out)

    assert {p.name: p.read_bytes() for p in out.iterdir()} == before
    assert [p.name for p in out.parent.iterdir()] == ["20260928"]
    assert store.latest_snapshot(out.parent) == out
