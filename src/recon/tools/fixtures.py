"""Synthetic fixture data for the Step 3 MCP tool server.

Companies, facts, and filings here are fictional — CLAUDE.md forbids naming
real companies, and this data isn't drawn from any real filing anyway. It
exists to exercise the tool contract (`docs/contracts.md` section 3) end to
end, not to answer finance-agent-bench questions correctly; see
`docs/data-sources.md` for the real-data gap this leaves.

`seed()` loads three tables into whatever DuckDB connection it's given:
`companies`, `financial_facts`, `filings`. A handful of rows are padding
(company id prefix `BULK-`, concept prefix `bulk_`) that exists only to push
a query past `MAX_ROWS` so the `truncated` status is reachable through a
real query instead of a test-only shortcut.
"""

import duckdb

# (company_id, name, sector, fiscal_year_end)
COMPANIES: list[tuple[str, str, str, str]] = [
    ("FIRM-001", "Aurora Robotics Corp", "Industrials", "12-31"),
    ("FIRM-002", "Blue Harbor Foods", "Consumer Staples", "12-31"),
    ("FIRM-003", "Cascade Semiconductors", "Technology", "09-30"),
    ("FIRM-004", "Driftwood Logistics", "Industrials", "12-31"),
    ("FIRM-005", "Ember Health Systems", "Healthcare", "06-30"),
    ("FIRM-006", "Fenwick Insurance Group", "Financials", "12-31"),
    ("FIRM-007", "Granite Materials Inc", "Materials", "12-31"),
    ("FIRM-008", "Harbor Point Energy", "Energy", "12-31"),
    # No facts and no filings — the "we know this company, we have nothing
    # on it" case for list_financial_concepts / get_financial_fact / search_filings.
    ("FIRM-009", "Ironwood Retail Partners", "Consumer Discretionary", "12-31"),
]

# Pushes list_companies(sector=None) past MAX_ROWS.
_BULK_COMPANY_COUNT = 520

_YEARS = [2022, 2023, 2024]
_PERIODS = ["FY", "Q1", "Q2", "Q3", "Q4"]
_CONCEPT_BASE_VALUES = {
    "revenue": 500.0,
    "net_income": 40.0,
    "gross_margin_pct": 42.0,
    "operating_expenses": 120.0,
}
_CONCEPT_UNITS = {
    "revenue": "USD_M",
    "net_income": "USD_M",
    "gross_margin_pct": "PCT",
    "operating_expenses": "USD_M",
}

# Pushes list_financial_concepts("FIRM-003") past MAX_ROWS.
_BULK_CONCEPT_COUNT = 520
# Pushes get_financial_fact("FIRM-004", "bulk_yearly_revenue", fiscal_year=None) past MAX_ROWS.
_BULK_FACT_YEAR_COUNT = 520
# Pushes search_filings("FIRM-002") with no filters past MAX_ROWS.
_BULK_FILING_COUNT = 520

# (company_id, form_type, fiscal_year, fiscal_period, filed_date, summary_text)
FILINGS: list[tuple[str, str, int, str, str, str]] = [
    (
        "FIRM-001",
        "10-K",
        2024,
        "FY",
        "2025-02-15",
        (
            "Aurora Robotics restructured its manufacturing footprint and "
            "flagged supply chain risk from a single sensor vendor."
        ),
    ),
    (
        "FIRM-001",
        "10-Q",
        2024,
        "Q3",
        "2024-11-01",
        (
            "Aurora Robotics reported a higher backlog and raised full-year "
            "guidance on strong industrial demand."
        ),
    ),
    (
        "FIRM-002",
        "10-K",
        2024,
        "FY",
        "2025-02-20",
        (
            "Blue Harbor Foods completed the acquisition of a regional snack "
            "brand and recorded transaction costs."
        ),
    ),
    (
        "FIRM-003",
        "10-K",
        2024,
        "FY",
        "2025-01-30",
        (
            "Cascade Semiconductors announced a capacity expansion at its "
            "main fabrication facility."
        ),
    ),
    (
        "FIRM-003",
        "10-Q",
        2024,
        "Q2",
        "2024-07-25",
        (
            "Cascade Semiconductors noted continued capacity expansion "
            "spending and softer near-term demand."
        ),
    ),
    (
        "FIRM-005",
        "10-K",
        2024,
        "FY",
        "2025-03-01",
        (
            "Ember Health Systems disclosed a cybersecurity incident "
            "affecting patient billing systems."
        ),
    ),
    (
        "FIRM-006",
        "10-Q",
        2024,
        "Q1",
        "2024-05-10",
        (
            "Fenwick Insurance Group increased loss reserves after severe "
            "weather claims."
        ),
    ),
    (
        "FIRM-007",
        "10-K",
        2024,
        "FY",
        "2025-02-10",
        "Granite Materials idled one quarry due to weak construction demand.",
    ),
    (
        "FIRM-008",
        "10-Q",
        2024,
        "Q4",
        "2025-02-05",
        (
            "Harbor Point Energy signed a long-term supply agreement for "
            "renewable feedstock."
        ),
    ),
]


def seed(conn: duckdb.DuckDBPyConnection) -> None:
    """Create and populate companies, financial_facts, and filings on `conn`."""
    _seed_companies(conn)
    _seed_financial_facts(conn)
    _seed_filings(conn)


def _seed_companies(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        "CREATE TABLE companies ("
        "company_id VARCHAR, name VARCHAR, sector VARCHAR, fiscal_year_end VARCHAR)"
    )
    conn.executemany("INSERT INTO companies VALUES (?, ?, ?, ?)", COMPANIES)
    # Generated in SQL, not via executemany, so seeding stays fast even at
    # this row count — see the module docstring for why this padding exists.
    conn.execute(
        "INSERT INTO companies "
        "SELECT 'BULK-' || lpad(i::VARCHAR, 4, '0'), "
        "'Padding Holdings ' || i, 'Diversified', '12-31' "
        "FROM range(?) t(i)",
        [_BULK_COMPANY_COUNT],
    )


def _seed_financial_facts(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        "CREATE TABLE financial_facts ("
        "company_id VARCHAR, fiscal_year INTEGER, fiscal_period VARCHAR, "
        "concept VARCHAR, value DOUBLE, unit VARCHAR)"
    )

    rows: list[tuple[str, int, str, str, float, str]] = []
    real_company_ids = [c[0] for c in COMPANIES if c[0] != "FIRM-009"]
    for company_id in real_company_ids:
        for year in _YEARS:
            for period_index, period in enumerate(_PERIODS):
                for concept, base in _CONCEPT_BASE_VALUES.items():
                    value = (
                        base + (year - 2022) * base * 0.05 + period_index * base * 0.01
                    )
                    rows.append(
                        (
                            company_id,
                            year,
                            period,
                            concept,
                            round(value, 2),
                            _CONCEPT_UNITS[concept],
                        )
                    )
    conn.executemany("INSERT INTO financial_facts VALUES (?, ?, ?, ?, ?, ?)", rows)

    conn.execute(
        "INSERT INTO financial_facts "
        "SELECT 'FIRM-003', 2024, 'FY', 'bulk_concept_' || lpad(i::VARCHAR, 4, '0'), "
        "i::DOUBLE, 'USD_M' FROM range(?) t(i)",
        [_BULK_CONCEPT_COUNT],
    )

    conn.execute(
        "INSERT INTO financial_facts "
        "SELECT 'FIRM-004', 1000 + i, 'FY', 'bulk_yearly_revenue', i::DOUBLE, 'USD_M' "
        "FROM range(?) t(i)",
        [_BULK_FACT_YEAR_COUNT],
    )


def _seed_filings(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        "CREATE TABLE filings ("
        "company_id VARCHAR, form_type VARCHAR, fiscal_year INTEGER, "
        "fiscal_period VARCHAR, filed_date VARCHAR, summary_text VARCHAR)"
    )
    conn.executemany("INSERT INTO filings VALUES (?, ?, ?, ?, ?, ?)", FILINGS)
    conn.execute(
        "INSERT INTO filings "
        "SELECT 'FIRM-002', '8-K', 2024, 'FY', '2024-01-01', "
        "'Routine filing update number ' || i || '.' FROM range(?) t(i)",
        [_BULK_FILING_COUNT],
    )
