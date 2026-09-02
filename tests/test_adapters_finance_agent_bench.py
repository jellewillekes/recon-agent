"""Tests for src/recon/adapters/finance_agent_bench.py.

Covers: correct field mapping from the real CSV schema, the case_id derivation,
that fetch_csv never touches the network in a test (mocked transport, and a
cache-hit path that doesn't request at all), and that a row the contract can't
accept (blank answer, this source never has a tool path) is rejected rather
than silently dropped.
"""

from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from recon.adapters.finance_agent_bench import (
    ATTRIBUTION,
    DEFAULT_CACHE_FILENAME,
    LICENSE,
    PINNED_COMMIT,
    RAW_CSV_URL,
    fetch_csv,
    load_cases,
)

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_CSV = FIXTURES / "finance_agent_bench_sample.csv"
MISSING_ANSWER_CSV = FIXTURES / "finance_agent_bench_missing_answer.csv"
MALFORMED_RUBRIC_CSV = FIXTURES / "finance_agent_bench_malformed_rubric.csv"
MISSING_COLUMN_CSV = FIXTURES / "finance_agent_bench_missing_column.csv"


@pytest.mark.unit
def test_load_cases_maps_every_row() -> None:
    cases = load_cases(SAMPLE_CSV)
    assert len(cases) == 3


@pytest.mark.unit
def test_load_cases_maps_fields_correctly() -> None:
    case = load_cases(SAMPLE_CSV)[0]

    assert case.case_id.startswith("finance-agent-bench:")
    assert case.source == "finance-agent-bench"
    assert (
        case.question
        == "What was the revenue trend for the retailer described in the filing?"
    )
    assert (
        case.expected_answer
        == "Revenue grew steadily each quarter, driven by higher unit sales."
    )
    assert case.expected_tool_path is None
    assert case.tags == ["Trends"]
    assert case.context["question_type"] == "Trends"
    assert case.context["expert_time_minutes"] == 12.0
    assert case.context["rubric"] == [
        {"operator": "correctness", "criteria": "Revenue grew each quarter"}
    ]
    assert case.license == LICENSE
    assert case.attribution == ATTRIBUTION


@pytest.mark.unit
def test_case_id_is_deterministic_and_order_independent() -> None:
    first_pass = {c.question: c.case_id for c in load_cases(SAMPLE_CSV)}
    second_pass = {c.question: c.case_id for c in load_cases(SAMPLE_CSV)}
    assert first_pass == second_pass


@pytest.mark.unit
def test_missing_answer_and_no_tool_path_is_rejected() -> None:
    """This source never sets expected_tool_path, so a blank answer leaves a
    case with neither — exactly the invariant Case itself enforces."""
    with pytest.raises(ValidationError):
        load_cases(MISSING_ANSWER_CSV)


@pytest.mark.unit
def test_malformed_rubric_json_raises_with_line_context() -> None:
    with pytest.raises(ValueError, match=r"line 2.*Rubric"):
        load_cases(MALFORMED_RUBRIC_CSV)


@pytest.mark.unit
def test_missing_column_raises_with_line_context() -> None:
    with pytest.raises(ValueError, match=r"line 2.*Expert time \(mins\)"):
        load_cases(MISSING_COLUMN_CSV)


@pytest.mark.unit
def test_default_cache_filename_is_keyed_on_the_pin() -> None:
    """A future pin bump must change the default cache path (see cli.py's
    DEFAULT_DATASET_PATH), or a stale cached file would be reused silently."""
    assert DEFAULT_CACHE_FILENAME == f"public-{PINNED_COMMIT[:12]}.csv"


@pytest.mark.unit
def test_fetch_csv_downloads_via_mocked_transport(tmp_path: Path) -> None:
    requested_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(200, content=SAMPLE_CSV.read_bytes())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    dest = tmp_path / "public.csv"

    result = fetch_csv(dest, client=client)

    assert result == dest
    assert dest.read_bytes() == SAMPLE_CSV.read_bytes()
    assert requested_urls == [RAW_CSV_URL]


@pytest.mark.unit
def test_fetch_csv_skips_request_when_already_cached(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("fetch_csv must not request a URL that's already cached")

    dest = tmp_path / "public.csv"
    dest.write_bytes(b"cached content")
    client = httpx.Client(transport=httpx.MockTransport(handler))

    fetch_csv(dest, client=client)

    assert dest.read_bytes() == b"cached content"


@pytest.mark.unit
def test_fetch_csv_leaves_no_partial_file_on_failure(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    dest = tmp_path / "public.csv"
    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(httpx.HTTPStatusError):
        fetch_csv(dest, client=client)

    assert not dest.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.unit
def test_fetch_csv_leaves_no_temp_file_behind_on_success(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=SAMPLE_CSV.read_bytes())

    dest = tmp_path / "public.csv"
    client = httpx.Client(transport=httpx.MockTransport(handler))

    fetch_csv(dest, client=client)

    assert list(tmp_path.iterdir()) == [dest]
