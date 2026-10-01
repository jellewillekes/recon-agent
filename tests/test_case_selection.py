"""Choosing which cases an eval runs: `eval/case_selection.py` and the case
ids `sec_edgar_tickers.read_case_ids` reads from the tickers files."""

from pathlib import Path

import pytest

from recon.adapters.sec_edgar_tickers import read_case_ids
from recon.contracts import Case
from recon.eval.case_selection import (
    read_case_file,
    select_cases,
    spread_across_companies,
)

pytestmark = pytest.mark.unit


def _case(case_id: str) -> Case:
    return Case(
        case_id=case_id,
        source="finance-agent-bench",
        question="q",
        expected_answer="a",
        expected_tool_path=None,
        context={"rubric": []},
        tags=[],
        license="MIT",
        attribution="attribution",
    )


CASES = [_case(cid) for cid in ["c3", "c1", "c4", "c2", "c5"]]


def test_read_case_file_skips_comments_and_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "cases.txt"
    path.write_text("# smoke set\nc1\n\nc2  # second\n")
    assert read_case_file(path) == ["c1", "c2"]


def test_select_cases_keeps_dataset_order() -> None:
    selected = select_cases(CASES, ["c2", "c3"])
    assert [c.case_id for c in selected] == ["c3", "c2"]


def test_select_cases_refuses_unknown_ids() -> None:
    with pytest.raises(ValueError, match="1 case id"):
        select_cases(CASES, ["c1", "nope"])


def test_spread_takes_one_single_company_case_per_company_in_id_order() -> None:
    by_ticker = {
        "AAA": ["c1", "c2"],
        "BBB": ["c2", "c3"],  # c2 compares two companies: skipped
        "CCC": ["c4"],
        "DDD": ["c5"],
    }
    assert spread_across_companies(CASES, by_ticker, 3) == ["c1", "c3", "c4"]


def test_spread_refuses_when_too_few_companies() -> None:
    with pytest.raises(ValueError, match="Only 2 single-company cases"):
        spread_across_companies(CASES, {"AAA": ["c1"], "BBB": ["c3"]}, 3)


def test_read_case_ids_merges_files_and_skips_missing_ones(tmp_path: Path) -> None:
    derived = tmp_path / "tickers-dataset.txt"
    derived.write_text(
        "# header\nAAA\t1\tname\tAlpha Corp\tc1,c2\nBBB\t2\tticker\tBeta Inc\tc3\n"
    )
    manual = tmp_path / "tickers.txt"
    manual.write_text("AAA\t1\tmanual\tAlpha Corp\nCCC\t3\tmanual\tGamma\n")
    result = read_case_ids([derived, manual, tmp_path / "missing.txt"])
    assert result == {"AAA": ["c1", "c2"], "BBB": ["c3"], "CCC": []}
