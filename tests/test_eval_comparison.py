"""Tests for `eval/comparison.py`, shared by `recon.cli compare` and
`GET /evals/compare`."""

from datetime import UTC, datetime

import pytest

from recon.contracts import CaseScore, EvalRun
from recon.eval.comparison import compare_runs
from recon.eval.thresholds import GateThresholds

LIMITS = GateThresholds(
    answer_score_noise_band=0.10,
    task_completion_max_case_drop=1,
    cost_max_relative_rise=0.20,
)


def _score(case_id: str) -> CaseScore:
    return CaseScore(
        case_id=case_id,
        task_completion=True,
        answer_score=0.6,
        tool_path_exact=True,
        tool_path_equivalent=True,
        tool_call_accuracy=1.0,
        rubric_scores={},
        cost_eur=0.2,
        elapsed_ms=1000,
        notes="",
    )


def _run(**overrides: object) -> EvalRun:
    defaults: dict[str, object] = {
        "run_id": "eval-off",
        "timestamp_utc": datetime.now(UTC),
        "dataset": "finance-agent-bench@pin",
        "dataset_license": "MIT",
        "dataset_attribution": "attribution",
        "runtime": "agent_sdk",
        "mode": "multi",
        "model_config_hash": "abc",
        "prompt_hashes": {"supervisor": "abc"},
        "rubric_version": "4",
        "case_scores": [_score("a"), _score("b")],
        "aggregate": {
            "task_completion_rate": 1.0,
            "answer_score_mean": 0.6,
            "citation_precision_mean": 0.9,
        },
        "total_cost_eur": 1.0,
        "tool_data_snapshot": "snap",
    }
    defaults.update(overrides)
    return EvalRun(**defaults)  # type: ignore[arg-type]


@pytest.mark.unit
def test_comparable_runs_get_a_verdict_per_gated_metric() -> None:
    candidate = _run(run_id="eval-on", routing=True, total_cost_eur=0.5)

    result = compare_runs(_run(), candidate, LIMITS)

    assert result.comparable and result.reasons == []
    verdicts = {m.name: m.verdict for m in result.metrics}
    assert verdicts["total_cost_eur"] == "better"
    assert verdicts["answer_score_mean"] == "same"
    assert verdicts["citation_precision_mean"] is None
    bands = {m.name: m.band for m in result.metrics}
    assert bands["answer_score_mean"] == "±0.10"
    assert bands["task_completion_rate"] == "±1 case"
    assert bands["total_cost_eur"] == "±20%"
    assert bands["citation_precision_mean"] is None
    routing = next(s for s in result.settings if s.name == "routing")
    assert (routing.baseline, routing.candidate) == ("off", "on")


@pytest.mark.unit
def test_runs_the_gate_refuses_get_reasons_and_no_verdicts() -> None:
    candidate = _run(run_id="eval-new", rubric_version="3")

    result = compare_runs(_run(), candidate, LIMITS)

    assert not result.comparable
    assert any("rubric_version differs" in reason for reason in result.reasons)
    assert all(metric.verdict is None for metric in result.metrics)
    # The numbers still show, so the page can display them next to the reasons.
    cost = next(m for m in result.metrics if m.name == "total_cost_eur")
    assert (cost.baseline, cost.candidate) == (1.0, 1.0)


@pytest.mark.unit
def test_a_metric_a_run_lacks_is_none() -> None:
    old = _run(run_id="eval-old", aggregate={"answer_score_mean": 0.5})

    result = compare_runs(old, _run(), LIMITS)

    precision = next(m for m in result.metrics if m.name == "citation_precision_mean")
    assert (precision.baseline, precision.candidate) == (None, 0.9)


def test_an_incomplete_candidate_is_refused_like_in_the_gate() -> None:
    """The gate refuses a candidate cut short by the session limit (#123), so
    the comparison mustn't hand out verdicts for it."""
    candidate = _run(
        run_id="eval-on",
        aggregate={
            "task_completion_rate": 1.0,
            "answer_score_mean": 0.6,
            "cases_skipped_at_session_limit": 1.0,
        },
    )

    result = compare_runs(_run(), candidate, LIMITS)

    assert not result.comparable
    assert any(
        "the candidate run stopped at the session limit" in r for r in result.reasons
    )
    assert all(metric.verdict is None for metric in result.metrics)
