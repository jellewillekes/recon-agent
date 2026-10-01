"""Tests for `eval/gate.py` — the docs/contracts.md §9 promotion gate."""

from datetime import UTC, datetime

import pytest

from recon.contracts import CaseScore, EvalRun
from recon.eval.gate import SKIPPED_AT_COST_CAP, check_gate


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
    assert check_gate(candidate, baseline) == []


@pytest.mark.unit
def test_fails_when_task_completion_rate_drops() -> None:
    baseline = _run(aggregate={"task_completion_rate": 0.9, "answer_score_mean": 0.8})
    candidate = _run(aggregate={"task_completion_rate": 0.8, "answer_score_mean": 0.8})

    failures = check_gate(candidate, baseline)

    assert any("task_completion_rate" in f for f in failures)


@pytest.mark.unit
def test_fails_when_answer_score_drops_more_than_two_percent() -> None:
    baseline = _run(aggregate={"task_completion_rate": 0.9, "answer_score_mean": 0.80})
    candidate = _run(aggregate={"task_completion_rate": 0.9, "answer_score_mean": 0.77})

    failures = check_gate(candidate, baseline)

    assert any("answer_score_mean" in f for f in failures)


@pytest.mark.unit
def test_passes_when_answer_score_drops_within_two_percent() -> None:
    baseline = _run(aggregate={"task_completion_rate": 0.9, "answer_score_mean": 0.80})
    candidate = _run(
        aggregate={"task_completion_rate": 0.9, "answer_score_mean": 0.789}
    )

    assert check_gate(candidate, baseline) == []


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

    failures = check_gate(candidate, baseline)

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

    assert check_gate(candidate, baseline) == []


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

    failures = check_gate(candidate, baseline)

    assert len(failures) == 1
    assert expected in failures[0]


@pytest.mark.unit
def test_incomparable_runs_skip_the_metric_rules() -> None:
    """A worse score on an incomparable run says nothing, so it isn't reported."""
    baseline = _run(rubric_version="2")
    candidate = _run(aggregate={"task_completion_rate": 0.1, "answer_score_mean": 0.1})

    failures = check_gate(candidate, baseline)

    assert failures and all("rubric_version" in f for f in failures)


@pytest.mark.unit
def test_case_set_message_counts_missing_and_extra_cases() -> None:
    baseline = _run(case_scores=[_score("a"), _score("b"), _score("c")])
    candidate = _run(case_scores=[_score("a"), _score("z")])

    (failure,) = check_gate(candidate, baseline)

    assert "2 baseline case(s) not run" in failure
    assert "1 case(s) not in the baseline" in failure


@pytest.mark.unit
def test_same_cases_in_a_different_order_are_comparable() -> None:
    baseline = _run(case_scores=[_score("a"), _score("b")])
    candidate = _run(run_id="eval-2", case_scores=[_score("b"), _score("a")])

    assert check_gate(candidate, baseline) == []


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
    failures = check_gate(runs["candidate"], runs["baseline"])
    assert len(failures) == 1
    assert f"the {capped} run stopped at its cost cap with 2 case(s)" in failures[0]
