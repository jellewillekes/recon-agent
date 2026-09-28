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


def open_tool_data(
    processed_dir: Path = DEFAULT_PROCESSED_DIR,
) -> duckdb.DuckDBPyConnection:
    """An in-memory DuckDB connection holding the configured tool data."""
    source = os.environ.get(TOOL_DATA_ENV, "edgar")
    conn = duckdb.connect(":memory:")
    if source == "fixture":
        seed(conn)
    elif source == "edgar":
        load_edgar(conn, latest_snapshot(processed_dir))
    else:
        raise ValueError(
            f"{TOOL_DATA_ENV}={source!r} is not a known data source. Use 'edgar' "
            "(real SEC data, needs `recon.cli edgar fetch`) or 'fixture'."
        )
    return conn
