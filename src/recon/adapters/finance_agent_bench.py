"""Adapter for the finance-agent-bench dataset.

Maps `vals-ai/finance-agent`'s `data/public.csv` onto `Case`. Map and validate
only — never interpret or fill in a value the source doesn't provide. See
`docs/data-sources.md` for the schema, the license/attribution text, and the
pinned commit this fetches.
"""

import csv
import hashlib
import json
from pathlib import Path

import httpx

from recon.contracts import Case

SOURCE = "finance-agent-bench"

PINNED_COMMIT = "8ba65f81ab759a8e0d44e72aabc5a47cf839d563"
RAW_CSV_URL = f"https://raw.githubusercontent.com/vals-ai/finance-agent/{PINNED_COMMIT}/data/public.csv"

LICENSE = "MIT"
ATTRIBUTION = (
    "Data from vals-ai/finance-agent (MIT License), Copyright (c) 2025 Vals AI, Inc. "
    "https://github.com/vals-ai/finance-agent — benchmark details at "
    "https://www.vals.ai/benchmarks/finance_agent"
)


def fetch_csv(dest: Path, *, client: httpx.Client | None = None) -> Path:
    """Download the pinned `public.csv` to `dest`, unless it's already cached there."""
    if dest.exists():
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    owns_client = client is None
    client = client or httpx.Client()
    try:
        response = client.get(RAW_CSV_URL, follow_redirects=True, timeout=30)
        response.raise_for_status()
        dest.write_bytes(response.content)
    finally:
        if owns_client:
            client.close()
    return dest


def _case_id(question: str) -> str:
    """Content-derived id: stable across re-fetches, independent of row order."""
    digest = hashlib.sha256(question.encode("utf-8")).hexdigest()[:12]
    return f"{SOURCE}:{digest}"


def _row_to_case(row: dict[str, str]) -> Case:
    return Case(
        case_id=_case_id(row["Question"]),
        source=SOURCE,
        question=row["Question"],
        expected_answer=row["Answer"].strip() or None,
        expected_tool_path=None,
        context={
            "question_type": row["Question Type"],
            "expert_time_minutes": float(row["Expert time (mins)"]),
            "rubric": json.loads(row["Rubric"]),
        },
        tags=[row["Question Type"]],
        license=LICENSE,
        attribution=ATTRIBUTION,
    )


def load_cases(csv_path: Path) -> list[Case]:
    """Read `public.csv` from `csv_path` and map every row to a `Case`.

    Pure mapping — takes a local path so tests never touch the network.
    """
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [_row_to_case(row) for row in reader]
