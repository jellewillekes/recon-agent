"""Tests for `eval/gate.py` — the docs/contracts.md §9 promotion gate."""

from datetime import UTC, datetime

import pytest

from recon.contracts import EvalRun
from recon.eval.gate import check_gate


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
