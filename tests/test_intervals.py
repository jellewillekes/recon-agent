"""Tests for `eval/intervals.py`: 95% intervals on a run's headline numbers.

Each interval is checked against a calculation done another way: textbook
values, a hand calculation, or the exact distribution of all resamples."""

import pytest

from recon.contracts import CaseScore
from recon.eval.intervals import (
    cost_per_correct_interval,
    mean_interval,
    t_critical,
    wilson_interval,
)

pytestmark = pytest.mark.unit


def _score(answer: float, cost: float = 0.1) -> CaseScore:
    return CaseScore(
        case_id="c",
        task_completion=True,
        answer_score=answer,
        tool_path_exact=True,
        tool_path_equivalent=True,
        tool_call_accuracy=1.0,
        rubric_scores={},
        cost_eur=cost,
        elapsed_ms=1,
        notes="",
    )


def test_wilson_matches_textbook_values() -> None:
    all_seven = wilson_interval(7, 7)
    assert all_seven is not None
    assert all_seven.low == pytest.approx(0.6457, abs=1e-4)
    assert all_seven.high == pytest.approx(1.0)
    half = wilson_interval(5, 10)
    assert half is not None
    assert half.low == pytest.approx(0.2366, abs=1e-4)
    assert half.high == pytest.approx(0.7634, abs=1e-4)


def test_wilson_with_no_cases_or_no_successes() -> None:
    assert wilson_interval(0, 0) is None
    none = wilson_interval(0, 7)
    assert none is not None
    assert none.low == pytest.approx(0.0)


def test_wilson_rejects_impossible_counts() -> None:
    with pytest.raises(ValueError, match="successes"):
        wilson_interval(8, 7)


def test_mean_interval_matches_a_hand_calculation() -> None:
    # mean 0.7, sample sd 0.2, se 0.2/sqrt(3); t(2) = 4.303.
    interval = mean_interval([0.5, 0.7, 0.9])
    assert interval is not None
    half = 4.303 * 0.2 / 3**0.5
    assert interval.low == pytest.approx(0.7 - half, abs=1e-3)
    assert interval.high == pytest.approx(0.7 + half, abs=1e-3)


def test_mean_interval_is_clipped_to_the_score_range() -> None:
    interval = mean_interval([0.5, 0.7, 0.9], bounds=(0.0, 1.0))
    assert interval is not None
    assert interval.high == 1.0


def test_mean_interval_needs_two_cases() -> None:
    assert mean_interval([]) is None
    assert mean_interval([0.8]) is None


def test_mean_interval_of_identical_scores_has_no_width() -> None:
    interval = mean_interval([0.6, 0.6, 0.6])
    assert interval is not None
    assert interval.low == interval.high == pytest.approx(0.6)


def test_t_critical_values() -> None:
    assert t_critical(7) == pytest.approx(2.365)
    assert t_critical(1) == pytest.approx(12.706)
    assert t_critical(10_000) == pytest.approx(1.96)
    with pytest.raises(ValueError):
        t_critical(0)


def test_cost_per_correct_matches_the_exact_resample_distribution() -> None:
    """Three cases costing 0.1 each, two correct. Of the 27 equally likely
    resamples, 1 has no correct case (excluded). The rest give 0.3 / c for c
    correct cases: 0.1 (8/26), 0.15 (12/26), 0.3 (6/26). The 2.5th percentile
    is 0.1 and the 97.5th is 0.3."""
    scores = [_score(0.9), _score(0.9), _score(0.3)]
    interval = cost_per_correct_interval(scores, correct_answer_score=0.5)
    assert interval is not None
    assert interval.low == pytest.approx(0.1)
    assert interval.high == pytest.approx(0.3)


def test_cost_per_correct_is_repeatable() -> None:
    scores = [_score(0.9, 0.2), _score(0.8, 0.1), _score(0.3, 0.3), _score(0.7, 0.2)]
    assert cost_per_correct_interval(scores, 0.5) == cost_per_correct_interval(
        scores, 0.5
    )


def test_cost_per_correct_is_none_when_no_case_is_correct() -> None:
    assert cost_per_correct_interval([_score(0.1), _score(0.2)], 0.5) is None
    assert cost_per_correct_interval([], 0.5) is None
