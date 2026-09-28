"""Storage helpers for normalized SEC EDGAR snapshots: writing Parquet,
finding the latest snapshot, and per-company row counts."""

import csv
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import duckdb

from recon.adapters.sec_edgar import MANIFEST_FILENAME


def write_parquet(
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
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(columns)
            for row in rows:
                writer.writerow(["" if v is None else v for v in row])
                count += 1
        column_spec = ", ".join(f"'{name}': '{kind}'" for name, kind in columns.items())
        csv_sql = csv_name.replace("'", "''")
        out_sql = tmp_parquet.replace("'", "''")
        # Every format detail is declared, and auto-detection is off: its
        # sniffer guesses the escape character and column types from a
        # sample, and real filing descriptions (embedded quotes, commas)
        # made it guess wrong.
        duckdb.execute(
            f"COPY (SELECT * FROM read_csv('{csv_sql}', auto_detect=false, "
            "header=true, delim=',', quote='\"', escape='\"', new_line='\\n', "
            f"columns={{{column_spec}}}, nullstr='')) TO '{out_sql}' (FORMAT parquet)"
        )
        os.replace(tmp_parquet, dest)
        return count
    finally:
        os.unlink(csv_name)
        if os.path.exists(tmp_parquet):
            os.unlink(tmp_parquet)


def latest_snapshot(processed_dir: Path) -> Path:
    """The most recent normalized snapshot directory."""
    candidates = sorted(
        p
        for p in processed_dir.glob("*")
        if not p.name.startswith(".") and (p / MANIFEST_FILENAME).exists()
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
