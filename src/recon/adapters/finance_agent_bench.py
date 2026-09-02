"""Adapter for the finance-agent-bench dataset.

Maps `vals-ai/finance-agent`'s `data/public.csv` onto `Case`. Map and validate
only — never interpret or fill in a value the source doesn't provide. See
`docs/data-sources.md` for the schema, the license/attribution text, and the
pinned commit this fetches.
"""

import csv
import hashlib
import json
import os
import tempfile
from pathlib import Path

import httpx

from recon.contracts import Case

SOURCE = "finance-agent-bench"

PINNED_COMMIT = "8ba65f81ab759a8e0d44e72aabc5a47cf839d563"
RAW_CSV_URL = f"https://raw.githubusercontent.com/vals-ai/finance-agent/{PINNED_COMMIT}/data/public.csv"

# The pin is part of the cache filename, not just a comment: bumping
# PINNED_COMMIT changes the path a caller using the default fetches into, so a
# file cached under an older pin is never mistaken for the current one.
DEFAULT_CACHE_FILENAME = f"public-{PINNED_COMMIT[:12]}.csv"

LICENSE = "MIT"
ATTRIBUTION = (
    "Data from vals-ai/finance-agent (MIT License), Copyright (c) 2025 Vals AI, Inc. "
    "https://github.com/vals-ai/finance-agent — benchmark details at "
    "https://www.vals.ai/benchmarks/finance_agent"
)


def fetch_csv(dest: Path, *, client: httpx.Client | None = None) -> Path:
    """Download the pinned `public.csv` to `dest`, unless it's already cached there.

    Callers that want the cache to track `PINNED_COMMIT` automatically should
    build `dest` from `DEFAULT_CACHE_FILENAME` (as the CLI does) rather than a
    fixed name — this function only checks whether `dest` itself exists, it
    doesn't inspect what commit a pre-existing file came from.
    """
    if dest.exists():
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    owns_client = client is None
    client = client or httpx.Client()
    try:
        response = client.get(RAW_CSV_URL, follow_redirects=True, timeout=30)
        response.raise_for_status()
        # Write via a temp file + atomic rename so a killed download never
        # leaves a partial file that a later run would treat as a valid cache.
        fd, tmp_name = tempfile.mkstemp(dir=dest.parent)
        try:
            with os.fdopen(fd, "wb") as tmp_file:
                tmp_file.write(response.content)
            os.replace(tmp_name, dest)
        except BaseException:
            os.unlink(tmp_name)
            raise
    finally:
        if owns_client:
            client.close()
    return dest


def _case_id(question: str) -> str:
    """Content-derived id: stable across re-fetches, independent of row order."""
    digest = hashlib.sha256(question.encode("utf-8")).hexdigest()[:12]
    return f"{SOURCE}:{digest}"


def _row_to_case(row: dict[str, str], line_number: int) -> Case:
    """Map one CSV row to a `Case`.

    Field access and parsing are wrapped separately from `Case(...)` itself,
    so a malformed row (wrong file, upstream schema drift after a pin bump)
    raises a `ValueError` naming the line and the field, while `Case`'s own
    validation — e.g. the answer-or-tool-path invariant — still surfaces as
    its own `ValidationError` rather than being swallowed into this wrapper.
    """
    try:
        question = row["Question"]
        answer = row["Answer"]
        question_type = row["Question Type"]
        expert_time_raw = row["Expert time (mins)"]
        rubric_raw = row["Rubric"]
    except KeyError as exc:
        raise ValueError(
            f"finance-agent-bench line {line_number}: missing column {exc}. Expected "
            "Question, Answer, Question Type, Expert time (mins), Rubric — check "
            "--path points at the right file and the upstream schema hasn't changed."
        ) from exc

    try:
        expert_time_minutes = float(expert_time_raw)
    except ValueError as exc:
        raise ValueError(
            f"finance-agent-bench line {line_number}: 'Expert time (mins)' is not a "
            f"number: {expert_time_raw!r}."
        ) from exc

    try:
        rubric = json.loads(rubric_raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"finance-agent-bench line {line_number}: 'Rubric' is not valid JSON: {exc}."
        ) from exc

    return Case(
        case_id=_case_id(question),
        source=SOURCE,
        question=question,
        expected_answer=answer.strip() or None,
        expected_tool_path=None,
        context={
            "question_type": question_type,
            "expert_time_minutes": expert_time_minutes,
            "rubric": rubric,
        },
        tags=[question_type],
        license=LICENSE,
        attribution=ATTRIBUTION,
    )


def load_cases(csv_path: Path) -> list[Case]:
    """Read `public.csv` from `csv_path` and map every row to a `Case`.

    Pure mapping — takes a local path so tests never touch the network.
    """
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        # line 1 is the header; the first data row is line 2.
        return [
            _row_to_case(row, line_number)
            for line_number, row in enumerate(reader, start=2)
        ]
