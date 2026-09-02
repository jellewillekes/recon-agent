"""Tests for src/recon/cli.py.

compute_dataset_stats is a pure function (no I/O), so it's tested directly
rather than through the CLI/argparse wiring or the network-fetching path.
"""

import pytest

from recon.cli import compute_dataset_stats
from recon.contracts import Case


def _case(
    case_id: str, tags: list[str], expected_tool_path: list[str] | None = None
) -> Case:
    return Case(
        case_id=case_id,
        source="finance-agent-bench",
        question="q",
        expected_answer="a",
        expected_tool_path=expected_tool_path,
        context={},
        tags=tags,
        license="MIT",
        attribution="attribution",
    )


@pytest.mark.unit
def test_compute_dataset_stats_counts_cases_and_tags() -> None:
    cases = [
        _case("1", ["Trends"]),
        _case("2", ["Trends"]),
        _case("3", ["Market Analysis"]),
    ]

    stats = compute_dataset_stats(cases)

    assert stats["case_count"] == 3
    assert stats["tag_counts"] == {"Trends": 2, "Market Analysis": 1}


@pytest.mark.unit
def test_compute_dataset_stats_counts_expected_tool_path() -> None:
    cases = [
        _case("1", ["Trends"], expected_tool_path=["search"]),
        _case("2", ["Trends"], expected_tool_path=None),
    ]

    stats = compute_dataset_stats(cases)

    assert stats["cases_with_expected_tool_path"] == 1


@pytest.mark.unit
def test_compute_dataset_stats_on_empty_dataset() -> None:
    stats = compute_dataset_stats([])

    assert stats["case_count"] == 0
    assert stats["tag_counts"] == {}
    assert stats["cases_with_expected_tool_path"] == 0
