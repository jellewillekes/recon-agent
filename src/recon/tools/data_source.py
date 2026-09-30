"""Which data the MCP tools query: real SEC EDGAR tables or the synthetic fixture.

`RECON_TOOL_DATA` chooses, defaulting to `edgar`. A missing EDGAR cache is an
error, never a silent fallback to the fixture: an evaluation would then score
the agent against fake data without anyone noticing. Containers set
`RECON_TOOL_DATA=fixture` explicitly, since they carry no EDGAR cache.
"""

import os
from pathlib import Path

import duckdb

from recon.adapters.sec_edgar import DEFAULT_PROCESSED_DIR
from recon.adapters.sec_edgar_store import latest_snapshot
from recon.tools.fixtures import seed

TOOL_DATA_ENV = "RECON_TOOL_DATA"
EDGAR_TABLES = ("companies", "concepts", "financial_facts", "filings")


def load_edgar(conn: duckdb.DuckDBPyConnection, snapshot_dir: Path) -> None:
    """Copy a normalized snapshot's Parquet tables into `conn`."""
    for table in EDGAR_TABLES:
        path = str(snapshot_dir / f"{table}.parquet").replace("'", "''")
        conn.execute(f"CREATE TABLE {table} AS SELECT * FROM read_parquet('{path}')")


FIXTURE_SNAPSHOT_ID = "fixture"


def _selected_source() -> str:
    source = os.environ.get(TOOL_DATA_ENV, "edgar")
    if source not in ("edgar", "fixture"):
        raise ValueError(
            f"{TOOL_DATA_ENV}={source!r} is not a known data source. Use 'edgar' "
            "(real SEC data, needs `recon.cli edgar fetch`) or 'fixture'."
        )
    return source


def tool_data_snapshot_id(processed_dir: Path = DEFAULT_PROCESSED_DIR) -> str:
    """Which tool data `open_tool_data` would load, as recorded on an `EvalRun`.

    `"fixture"` for the synthetic data, else the EDGAR snapshot's id (its
    directory name, the fetch date). Two runs against different snapshots
    aren't comparable, so the promotion gate checks this. Raises like
    `open_tool_data` when the EDGAR cache is missing.
    """
    if _selected_source() == "fixture":
        return FIXTURE_SNAPSHOT_ID
    return latest_snapshot(processed_dir).name


def open_tool_data(
    processed_dir: Path = DEFAULT_PROCESSED_DIR,
) -> duckdb.DuckDBPyConnection:
    """An in-memory DuckDB connection holding the configured tool data."""
    conn = duckdb.connect(":memory:")
    if _selected_source() == "fixture":
        seed(conn)
    else:
        load_edgar(conn, latest_snapshot(processed_dir))
    return conn
