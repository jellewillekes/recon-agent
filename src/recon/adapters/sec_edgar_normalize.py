"""Normalize a fetched SEC EDGAR snapshot into Parquet tables for DuckDB.

Map and validate only: values come from the source as filed, nothing is
computed or filled in. The derivation rules (comparative-year dedup,
restatements, fiscal periods, the cutoff) are in
`docs/adr/0015-sec-edgar-fact-derivation.md`.

Tables, one Parquet file each under `<processed_dir>/<snapshot>/`:

- `companies`: one row per company in the reviewed tickers file
- `concepts`: every concept a company reports, with SEC's label/description
- `financial_facts`: one row per (company, taxonomy, concept, unit, period)
- `filings`: filing metadata for the forms listed in the config
"""

import csv
import json
import os
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Iterator
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from recon.adapters.sec_edgar import MANIFEST_FILENAME, EdgarConfig, atomic_write

_YEAR_DAYS = range(350, 381)
_QUARTER_DAYS = range(80, 101)
_QUARTERS = frozenset({"Q1", "Q2", "Q3"})

_COMPANIES_COLUMNS = {
    "company_id": "VARCHAR",
    "cik": "BIGINT",
    "name": "VARCHAR",
    "sector": "VARCHAR",
    "fiscal_year_end": "VARCHAR",
}
_CONCEPTS_COLUMNS = {
    "company_id": "VARCHAR",
    "taxonomy": "VARCHAR",
    "concept": "VARCHAR",
    "label": "VARCHAR",
    "description": "VARCHAR",
    "units": "VARCHAR",
}
_FACTS_COLUMNS = {
    "company_id": "VARCHAR",
    "taxonomy": "VARCHAR",
    "concept": "VARCHAR",
    "unit": "VARCHAR",
    "value": "DOUBLE",
    "period_start": "DATE",
    "period_end": "DATE",
    "period_type": "VARCHAR",
    "duration_days": "INTEGER",
    "fiscal_year": "INTEGER",
    "fiscal_period": "VARCHAR",
    "form": "VARCHAR",
    "filed": "DATE",
    "accession": "VARCHAR",
}
_FILINGS_COLUMNS = {
    "company_id": "VARCHAR",
    "accession": "VARCHAR",
    "form_type": "VARCHAR",
    "filed_date": "DATE",
    "report_date": "DATE",
    "fiscal_year": "INTEGER",
    "fiscal_period": "VARCHAR",
    "primary_document": "VARCHAR",
    "items": "VARCHAR",
    "summary_text": "VARCHAR",
}


def _load_json(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(path.read_bytes())
    return payload


def _filing_arrays(snapshot_dir: Path, cik: int) -> Iterator[dict[str, Any]]:
    """Every filing in the main submissions file and its older pages."""
    main_path = snapshot_dir / "submissions" / f"CIK{cik:010d}.json"
    if not main_path.exists():
        return
    main = _load_json(main_path)
    pages = [main["filings"]["recent"]]
    for page in main["filings"].get("files", []):
        page_path = snapshot_dir / "submissions" / page["name"]
        if page_path.exists():
            pages.append(_load_json(page_path))
    for arrays in pages:
        keys = list(arrays)
        for values in zip(*(arrays[k] for k in keys), strict=True):
            yield dict(zip(keys, values, strict=True))


def _report_dates(filings: list[dict[str, Any]]) -> dict[str, str]:
    """Accession -> period-of-report date, for every filing that has one."""
    return {
        f["accessionNumber"]: f["reportDate"] for f in filings if f.get("reportDate")
    }


def _fiscal_period(fp: str | None, start: str | None, end: str) -> str | None:
    """The period a current-period fact covers, derived from its dates.

    `fp` is the reporting filing's own period. A 10-K (`FY`) reports both the
    full year and, sometimes, its fourth quarter, told apart by duration. A
    10-Q's six- and nine-month year-to-date facts get `None`: they are not a
    quarter, and computing one from them would be filling in a value.
    """
    if start is None:
        return fp if fp == "FY" or fp in _QUARTERS else None
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    if fp == "FY":
        if days in _YEAR_DAYS:
            return "FY"
        return "Q4" if days in _QUARTER_DAYS else None
    if fp in _QUARTERS and days in _QUARTER_DAYS:
        return fp
    return None


def _fact_rows(
    company_id: str,
    taxonomy: str,
    concept: str,
    unit: str,
    entries: list[dict[str, Any]],
    report_dates: dict[str, str],
    cutoff: date,
) -> Iterator[tuple[Any, ...]]:
    """One row per period. The latest filing on or before `cutoff` supplies the
    value (restatements win). The filing where the period was the current one
    supplies `fiscal_year`/`fiscal_period`: `fy`/`fp` describe the filing, so
    a comparative year carried in a later filing must not take its labels."""
    by_period: dict[tuple[str | None, str], list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        if date.fromisoformat(entry["filed"]) <= cutoff:
            by_period[(entry.get("start"), entry["end"])].append(entry)
    for (start, end), group in by_period.items():
        latest = max(group, key=lambda e: (e["filed"], e["accn"]))
        current = [e for e in group if report_dates.get(e["accn"]) == end]
        origin = min(current, key=lambda e: e["filed"]) if current else None
        fp = origin.get("fp") if origin else None
        days = (
            (date.fromisoformat(end) - date.fromisoformat(start)).days
            if start
            else None
        )
        yield (
            company_id,
            taxonomy,
            concept,
            unit,
            float(latest["val"]),
            start,
            end,
            "instant" if start is None else "duration",
            days,
            origin.get("fy") if origin else None,
            _fiscal_period(fp, start, end) if origin else None,
            latest.get("form"),
            latest["filed"],
            latest["accn"],
        )


def _company_facts(
    company_id: str,
    facts_path: Path,
    report_dates: dict[str, str],
    config: EdgarConfig,
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]], dict[str, tuple[Any, Any]]]:
    """Fact rows, concept rows, and accession -> (fy, fp) for one company."""
    facts: list[tuple[Any, ...]] = []
    concepts: list[tuple[Any, ...]] = []
    accession_periods: dict[str, tuple[Any, Any]] = {}
    if not facts_path.exists():
        return facts, concepts, accession_periods
    payload = _load_json(facts_path).get("facts", {})
    for taxonomy in config.taxonomies:
        for concept, body in payload.get(taxonomy, {}).items():
            units = body.get("units", {})
            concepts.append(
                (
                    company_id,
                    taxonomy,
                    concept,
                    body.get("label"),
                    body.get("description"),
                    ",".join(sorted(units)),
                )
            )
            for unit, entries in units.items():
                for entry in entries:
                    accession_periods.setdefault(
                        entry["accn"], (entry.get("fy"), entry.get("fp"))
                    )
                facts.extend(
                    _fact_rows(
                        company_id,
                        taxonomy,
                        concept,
                        unit,
                        entries,
                        report_dates,
                        config.filed_cutoff,
                    )
                )
    return facts, concepts, accession_periods


def _filing_rows(
    company_id: str,
    filings: list[dict[str, Any]],
    accession_periods: dict[str, tuple[Any, Any]],
    config: EdgarConfig,
) -> Iterator[tuple[Any, ...]]:
    for filing in filings:
        if filing["form"] not in config.forms:
            continue
        if date.fromisoformat(filing["filingDate"]) > config.filed_cutoff:
            continue
        fy, fp = accession_periods.get(filing["accessionNumber"], (None, None))
        items = filing.get("items") or None
        description = filing.get("primaryDocDescription") or filing["form"]
        summary = f"{description}; items {items}" if items else description
        yield (
            company_id,
            filing["accessionNumber"],
            filing["form"],
            filing["filingDate"],
            filing.get("reportDate") or None,
            fy,
            fp,
            filing.get("primaryDocument") or None,
            items,
            summary,
        )


def _company_row(company_id: str, cik: int, snapshot_dir: Path) -> tuple[Any, ...]:
    main_path = snapshot_dir / "submissions" / f"CIK{cik:010d}.json"
    main = _load_json(main_path) if main_path.exists() else {}
    fye = main.get("fiscalYearEnd") or ""
    return (
        company_id,
        cik,
        main.get("name"),
        main.get("sicDescription") or None,
        f"{fye[:2]}-{fye[2:]}" if len(fye) == 4 else None,
    )


def _write_parquet(
    rows: Iterable[tuple[Any, ...]], columns: dict[str, str], dest: Path
) -> int:
    """Stage `rows` as CSV, then have DuckDB type and write them as Parquet.

    DuckDB's own CSV reader is far faster than row-by-row inserts for the
    millions of facts a full snapshot holds. Written to a temp file first,
    then renamed, like every other cache write.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, csv_name = tempfile.mkstemp(dir=dest.parent, suffix=".csv")
    tmp_parquet = f"{dest}.tmp"
    try:
        count = 0
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            for row in rows:
                writer.writerow(["" if v is None else v for v in row])
                count += 1
        column_spec = ", ".join(f"'{name}': '{kind}'" for name, kind in columns.items())
        csv_sql = csv_name.replace("'", "''")
        out_sql = tmp_parquet.replace("'", "''")
        duckdb.execute(
            f"COPY (SELECT * FROM read_csv('{csv_sql}', header=true, "
            f"columns={{{column_spec}}}, nullstr='')) TO '{out_sql}' (FORMAT parquet)"
        )
        os.replace(tmp_parquet, dest)
        return count
    finally:
        os.unlink(csv_name)
        if os.path.exists(tmp_parquet):
            os.unlink(tmp_parquet)


def normalize_snapshot(
    snapshot_dir: Path,
    tickers: dict[str, int],
    config: EdgarConfig,
    out_dir: Path,
) -> dict[str, int]:
    """Build the four Parquet tables for `tickers`. Returns row counts per table,
    which also go to `out_dir/manifest.json` with the raw manifest."""
    companies: list[tuple[Any, ...]] = []
    concepts: list[tuple[Any, ...]] = []
    filings: list[tuple[Any, ...]] = []

    def facts() -> Iterator[tuple[Any, ...]]:
        # Streamed one company at a time: a full snapshot holds millions of
        # facts, so the other tables are collected as a side effect instead
        # of holding every fact in memory at once.
        for company_id, cik in sorted(tickers.items()):
            raw_filings = list(_filing_arrays(snapshot_dir, cik))
            company_facts, company_concepts, accession_periods = _company_facts(
                company_id,
                snapshot_dir / "companyfacts" / f"CIK{cik:010d}.json",
                _report_dates(raw_filings),
                config,
            )
            companies.append(_company_row(company_id, cik, snapshot_dir))
            concepts.extend(company_concepts)
            filings.extend(
                _filing_rows(company_id, raw_filings, accession_periods, config)
            )
            yield from company_facts

    fact_count = _write_parquet(
        facts(), _FACTS_COLUMNS, out_dir / "financial_facts.parquet"
    )
    counts = {
        "companies": _write_parquet(
            companies, _COMPANIES_COLUMNS, out_dir / "companies.parquet"
        ),
        "concepts": _write_parquet(
            concepts, _CONCEPTS_COLUMNS, out_dir / "concepts.parquet"
        ),
        "financial_facts": fact_count,
        "filings": _write_parquet(
            filings, _FILINGS_COLUMNS, out_dir / "filings.parquet"
        ),
    }
    _write_processed_manifest(snapshot_dir, out_dir, config, counts)
    return counts


def _write_processed_manifest(
    snapshot_dir: Path, out_dir: Path, config: EdgarConfig, counts: dict[str, int]
) -> None:
    """Carry the raw manifest forward, with the cutoff and row counts."""
    raw_manifest_path = snapshot_dir / MANIFEST_FILENAME
    manifest = {
        "source": _load_json(raw_manifest_path) if raw_manifest_path.exists() else None,
        "filed_cutoff": config.filed_cutoff.isoformat(),
        "row_counts": counts,
    }
    atomic_write(
        out_dir / MANIFEST_FILENAME, json.dumps(manifest, indent=2).encode("utf-8")
    )


def latest_snapshot(processed_dir: Path) -> Path:
    """The most recent normalized snapshot directory."""
    candidates = sorted(
        p for p in processed_dir.glob("*") if (p / MANIFEST_FILENAME).exists()
    )
    if not candidates:
        raise FileNotFoundError(
            f"No normalized SEC EDGAR snapshot under {processed_dir}. Run "
            "`recon.cli edgar fetch` first."
        )
    return candidates[-1]


def company_stats(snapshot_dir: Path) -> list[tuple[str, int, int]]:
    """(company_id, fact count, filing count) per company in a snapshot."""
    path = str(snapshot_dir).replace("'", "''")
    rows = duckdb.execute(
        f"""
        SELECT c.company_id,
               (SELECT count(*) FROM read_parquet('{path}/financial_facts.parquet') f
                WHERE f.company_id = c.company_id),
               (SELECT count(*) FROM read_parquet('{path}/filings.parquet') g
                WHERE g.company_id = c.company_id)
        FROM read_parquet('{path}/companies.parquet') c
        ORDER BY c.company_id
        """
    ).fetchall()
    return [(str(r[0]), int(r[1]), int(r[2])) for r in rows]
