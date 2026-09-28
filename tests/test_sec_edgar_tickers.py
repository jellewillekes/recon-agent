"""Tests for `adapters/sec_edgar_tickers.py`. Every company here is fictional."""

from pathlib import Path

import pytest

from recon.adapters import sec_edgar_tickers as tickers
from recon.contracts import Case

COMPANY_TICKERS = [
    ("EXWD", 1001, "Example Widgets, Inc."),
    ("NBRT", 1002, "Northbridge Retail Holdings Corp"),
    ("HRBR", 1003, "Harbor Co"),
    ("QZ", 1004, "Quartz Analytics Inc"),
    ("GAAP", 1005, "Some Fund That Uses An Acronym Ticker"),
    ("ZNTH", 1006, "The Zenith & Partners Company"),
]


def _case(question: str, case_id: str) -> Case:
    return Case(
        case_id=case_id,
        source="test",
        question=question,
        expected_answer="a",
        expected_tool_path=None,
        context={},
        tags=["t"],
        license="MIT",
        attribution="a",
    )


def _derive(*questions: str) -> dict[int, tickers.TickerMatch]:
    cases = [_case(q, f"c{i}") for i, q in enumerate(questions)]
    return tickers.derive_tickers(cases, COMPANY_TICKERS)


@pytest.mark.unit
def test_exchange_ticker_is_matched() -> None:
    matches = _derive("What was revenue at the firm (NASDAQ: EXWD) in 2024?")
    assert matches[1001].matched_by == {"ticker"}


@pytest.mark.unit
def test_parenthesized_and_bare_tickers_are_matched() -> None:
    matches = _derive("Compare QZ to its peer (NBRT).")
    assert set(matches) == {1002, 1004}


@pytest.mark.unit
def test_finance_acronyms_are_not_tickers() -> None:
    assert _derive("What was GAAP gross margin?") == {}


@pytest.mark.unit
def test_registered_name_matches_without_suffix() -> None:
    matches = _derive("How did Example Widgets grow in FY2024?")
    assert matches[1001].matched_by == {"name"}


@pytest.mark.unit
def test_name_with_leading_the_and_ampersand_matches() -> None:
    matches = _derive("Summarize Zenith and Partners guidance.")
    assert 1006 in matches


@pytest.mark.unit
def test_lowercase_prose_does_not_match_a_name() -> None:
    assert _derive("Ships waited in the harbor for weeks.") == {}


@pytest.mark.unit
def test_both_methods_and_cases_accumulate() -> None:
    matches = _derive("How did Example Widgets do?", "What did EXWD guide for Q3?")
    assert matches[1001].matched_by == {"name", "ticker"}
    assert matches[1001].case_ids == {"c0", "c1"}


@pytest.mark.unit
def test_unmatched_case_ids() -> None:
    cases = [_case("Example Widgets revenue?", "c0"), _case("Who won?", "c1")]
    matches = tickers.derive_tickers(cases, COMPANY_TICKERS)
    assert tickers.unmatched_case_ids(cases, matches) == ["c1"]


@pytest.mark.unit
def test_tickers_file_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "tickers.txt"
    matches = _derive("How did Example Widgets do?", "Revenue at QZ?")
    tickers.write_tickers_file(path, matches, ["c9"])

    assert tickers.read_tickers_file(path) == {"EXWD": 1001, "QZ": 1004}
    assert "# unmatched: c9" in path.read_text(encoding="utf-8")


@pytest.mark.unit
def test_tickers_file_is_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "tickers.txt"
    path.write_text("EXWD\t1001\n", encoding="utf-8")
    with pytest.raises(FileExistsError, match="manual edits"):
        tickers.write_tickers_file(path, {}, [])


@pytest.mark.unit
def test_manual_rows_and_comments_are_read(tmp_path: Path) -> None:
    path = tmp_path / "tickers.txt"
    path.write_text("# comment\n\nABCD\t42\n", encoding="utf-8")
    assert tickers.read_tickers_file(path) == {"ABCD": 42}


@pytest.mark.unit
def test_malformed_row_names_the_line(tmp_path: Path) -> None:
    path = tmp_path / "tickers.txt"
    path.write_text("ABCD\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 1"):
        tickers.read_tickers_file(path)


@pytest.mark.unit
def test_missing_file_says_how_to_create_it(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="--from-dataset"):
        tickers.read_tickers_file(tmp_path / "tickers.txt")
