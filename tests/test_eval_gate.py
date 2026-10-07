"""Tests for `eval/gate.py` — the docs/contracts.md §9 promotion gate."""

from datetime import UTC, datetime

import pytest

from recon.contracts import CaseScore, EvalRun
from recon.eval.gate import SKIPPED_AT_COST_CAP, check_gate, verdicts
from recon.eval.thresholds import GateThresholds

# Fixed here, not read from config/thresholds.yaml, so these tests don't
# change meaning when the user changes the file.
LIMITS = GateThresholds(
    answer_score_noise_band=0.10,
    task_completion_max_case_drop=1,
    cost_max_relative_rise=0.20,
)


def _run(**overrides: object) -> EvalRun:
    defaults: dict[str, object] = {
        "run_id": "eval-1",
        "timestamp_utc": datetime.now(UTC),
        "dataset": "finance-agent-bench",
        "dataset_license": "MIT",
        "dataset_attribution": "attribution",
        "runtime": "agent_sdk",
        "mode": "single",
        "model_config_hash": "abc",
        "prompt_hashes": {"investigator": "abc"},
        "rubric_version": "1",
        "case_scores": [],
        "aggregate": {"task_completion_rate": 1.0, "answer_score_mean": 1.0},
        "total_cost_eur": 0.10,
        "tool_data_snapshot": "fixture",
    }
    defaults.update(overrides)
    return EvalRun(**defaults)  # type: ignore[arg-type]


@pytest.mark.unit
def test_no_failures_when_candidate_matches_baseline() -> None:
    baseline = _run()
    candidate = _run(run_id="eval-2")
    assert check_gate(candidate, baseline, LIMITS) == []


@pytest.mark.unit
def test_fails_when_task_completion_rate_drops() -> None:
    baseline = _run(aggregate={"task_completion_rate": 0.9, "answer_score_mean": 0.8})
    candidate = _run(aggregate={"task_completion_rate": 0.8, "answer_score_mean": 0.8})

    failures = check_gate(candidate, baseline, LIMITS)

    assert any("task_completion_rate" in f for f in failures)


@pytest.mark.unit
def test_fails_when_answer_score_drops_more_than_the_noise_band() -> None:
    baseline = _run(aggregate={"task_completion_rate": 0.9, "answer_score_mean": 0.80})
    candidate = _run(aggregate={"task_completion_rate": 0.9, "answer_score_mean": 0.69})

    failures = check_gate(candidate, baseline, LIMITS)

    assert any("answer_score_mean" in f for f in failures)


@pytest.mark.unit
def test_passes_when_answer_score_drops_within_the_noise_band() -> None:
    """#77: two identical runs differed by 0.044, and one case can move a
    7-case mean by about 0.07."""
    baseline = _run(aggregate={"task_completion_rate": 0.9, "answer_score_mean": 0.80})
    candidate = _run(aggregate={"task_completion_rate": 0.9, "answer_score_mean": 0.71})

    assert check_gate(candidate, baseline, LIMITS) == []


@pytest.mark.unit
def test_fails_when_cost_rises_more_than_twenty_percent_without_completion_rise() -> (
    None
):
    baseline = _run(
        aggregate={"task_completion_rate": 0.9, "answer_score_mean": 0.9},
        total_cost_eur=1.00,
    )
    candidate = _run(
        aggregate={"task_completion_rate": 0.9, "answer_score_mean": 0.9},
        total_cost_eur=1.30,
    )

    failures = check_gate(candidate, baseline, LIMITS)

    assert any("total_cost_eur" in f for f in failures)


@pytest.mark.unit
def test_cost_rise_allowed_when_completion_also_rises() -> None:
    baseline = _run(
        aggregate={"task_completion_rate": 0.8, "answer_score_mean": 0.9},
        total_cost_eur=1.00,
    )
    candidate = _run(
        aggregate={"task_completion_rate": 0.95, "answer_score_mean": 0.9},
        total_cost_eur=1.30,
    )

    assert check_gate(candidate, baseline, LIMITS) == []


# --- comparability: refused before any metric (docs/contracts.md §9) --------


def _score(case_id: str) -> CaseScore:
    return CaseScore(
        case_id=case_id,
        task_completion=True,
        answer_score=1.0,
        tool_path_exact=False,
        tool_path_equivalent=False,
        tool_call_accuracy=1.0,
        rubric_scores={},
        cost_eur=0.05,
        elapsed_ms=1,
        notes="",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("baseline_overrides", "expected"),
    [
        ({"rubric_version": "2"}, "rubric_version differs"),
        ({"dataset": "other-dataset"}, "dataset differs"),
        ({"tool_data_snapshot": "20260928"}, "tool_data_snapshot differs"),
        ({"tool_data_snapshot": None}, "doesn't record which tool data"),
        ({"case_scores": [_score("a"), _score("b")]}, "case sets differ"),
    ],
    ids=["rubric", "dataset", "snapshot", "no-snapshot", "cases"],
)
def test_refuses_incomparable_runs(
    baseline_overrides: dict[str, object], expected: str
) -> None:
    baseline = _run(**baseline_overrides)
    candidate = _run(
        run_id="eval-2",
        case_scores=[_score("a")] if "case_scores" in baseline_overrides else [],
    )

    failures = check_gate(candidate, baseline, LIMITS)

    assert len(failures) == 1
    assert expected in failures[0]


@pytest.mark.unit
def test_incomparable_runs_skip_the_metric_rules() -> None:
    """A worse score on an incomparable run says nothing, so it isn't reported."""
    baseline = _run(rubric_version="2")
    candidate = _run(aggregate={"task_completion_rate": 0.1, "answer_score_mean": 0.1})

    failures = check_gate(candidate, baseline, LIMITS)

    assert failures and all("rubric_version" in f for f in failures)


@pytest.mark.unit
def test_case_set_message_counts_missing_and_extra_cases() -> None:
    baseline = _run(case_scores=[_score("a"), _score("b"), _score("c")])
    candidate = _run(case_scores=[_score("a"), _score("z")])

    (failure,) = check_gate(candidate, baseline, LIMITS)

    assert "2 baseline case(s) not run" in failure
    assert "1 case(s) not in the baseline" in failure


@pytest.mark.unit
def test_same_cases_in_a_different_order_are_comparable() -> None:
    baseline = _run(case_scores=[_score("a"), _score("b")])
    candidate = _run(run_id="eval-2", case_scores=[_score("b"), _score("a")])

    assert check_gate(candidate, baseline, LIMITS) == []


@pytest.mark.unit
@pytest.mark.parametrize("capped", ["candidate", "baseline"])
def test_refuses_a_run_stopped_at_its_cost_cap(capped: str) -> None:
    """A capped run didn't measure its whole case set, whichever side it's on."""
    aggregate = {
        "task_completion_rate": 1.0,
        "answer_score_mean": 1.0,
        SKIPPED_AT_COST_CAP: 2.0,
    }
    runs = {"candidate": _run(run_id="eval-2"), "baseline": _run()}
    runs[capped] = _run(run_id=f"eval-{capped}", aggregate=aggregate)
    failures = check_gate(runs["candidate"], runs["baseline"], LIMITS)
    assert len(failures) == 1
    assert f"the {capped} run stopped at its cost cap with 2 case(s)" in failures[0]


# --- noise (#77, docs/eval-noise.md) -----------------------------------------


def _measured(completed: int, answer: float, cost: float) -> EvalRun:
    """A 7-case run shaped like the two identical runs in docs/eval-noise.md."""
    scores = [
        _score(f"case-{i}").model_copy(update={"task_completion": i < completed})
        for i in range(7)
    ]
    return _run(
        case_scores=scores,
        aggregate={
            "task_completion_rate": completed / 7,
            "answer_score_mean": answer,
            "case_count": 7.0,
        },
        total_cost_eur=cost,
    )


@pytest.mark.unit
def test_two_identical_runs_pass_the_gate_both_ways() -> None:
    """The old gate rejected an unchanged repeat of the baseline in both
    directions: one case's completion, and a 2% relative score drop."""
    first = _measured(7, 0.652, 1.336)
    second = _measured(6, 0.696, 1.228)

    assert check_gate(second, first, LIMITS) == []
    assert check_gate(first, second, LIMITS) == []
    assert set(verdicts(second, first, LIMITS).values()) == {"same"}


@pytest.mark.unit
def test_two_more_incomplete_cases_fail_the_gate() -> None:
    failures = check_gate(_measured(5, 0.652, 1.3), _measured(7, 0.652, 1.3), LIMITS)

    assert any("task_completion_rate" in f for f in failures)


@pytest.mark.unit
def test_verdicts_name_a_change_beyond_the_noise() -> None:
    baseline = _measured(6, 0.60, 1.0)
    candidate = _measured(6, 0.75, 1.5)

    assert verdicts(candidate, baseline, LIMITS) == {
        "answer_score_mean": "better",
        "task_completion_rate": "same",
        "total_cost_eur": "worse",
    }
