"""Intervals and the noise rule where a reader sees them: the markdown
summary, the comparison, `GET /evals/compare` and the gate's own wording."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from recon.api import evals as api_evals
from recon.api import main as api_main
from recon.contracts import CaseScore, EvalRun
from recon.eval.comparison import compare_runs
from recon.eval.gate import noise_rule
from recon.eval.report import render_markdown
from recon.eval.thresholds import GateThresholds, load_thresholds

LIMITS = GateThresholds(
    answer_score_noise_band=0.10,
    task_completion_max_case_drop=1,
    cost_max_relative_rise=0.20,
)


def _run(run_id: str, answers: list[float], **overrides: Any) -> EvalRun:
    fields: dict[str, Any] = {
        "run_id": run_id,
        "timestamp_utc": datetime.now(UTC),
        "dataset": "finance-agent-bench@pin",
        "dataset_license": "MIT",
        "dataset_attribution": "attribution",
        "runtime": "agent_sdk",
        "mode": "single",
        "model_config_hash": "abc",
        "prompt_hashes": {"investigator": "abc"},
        "rubric_version": "4",
        "case_scores": [
            CaseScore(
                case_id=f"case-{i}",
                task_completion=True,
                answer_score=answer,
                tool_path_exact=True,
                tool_path_equivalent=True,
                tool_call_accuracy=1.0,
                rubric_scores={},
                cost_eur=0.1,
                elapsed_ms=1,
                notes="",
            )
            for i, answer in enumerate(answers)
        ],
        "aggregate": {
            "task_completion_rate": 1.0,
            "answer_score_mean": sum(answers) / len(answers),
        },
        "total_cost_eur": 0.1 * len(answers),
        "tool_data_snapshot": "snap",
    }
    fields.update(overrides)
    return EvalRun(**fields)


NOISY_A = [0.9, 0.2, 0.8, 0.3, 0.7]
NOISY_B = [0.3, 0.8, 0.2, 0.9, 0.6]


@pytest.mark.unit
def test_the_summary_shows_intervals_for_the_headline_numbers() -> None:
    run = _run("r", NOISY_A)
    run.aggregate["cost_per_correct_answer_eur"] = 0.2
    md = render_markdown(run, correct_answer_score=0.5)
    assert "## Uncertainty" in md
    assert "task_completion_rate" in md
    assert "answer_score_mean" in md
    assert "cost_per_correct_answer_eur" in md
    assert "95%" in md


@pytest.mark.unit
def test_the_summary_without_a_cutoff_skips_only_cost_per_correct() -> None:
    md = render_markdown(_run("r", NOISY_A))
    assert "answer_score_mean" in md
    assert "cost_per_correct_answer_eur" not in md.split("## Uncertainty")[1]


@pytest.mark.unit
def test_the_summary_of_one_case_says_no_interval() -> None:
    md = render_markdown(_run("r", [0.8]))
    assert "needs at least two cases" in md


@pytest.mark.unit
def test_the_gate_states_its_noise_rule() -> None:
    rule = noise_rule(LIMITS)
    assert "0.10" in rule
    assert "config/thresholds.yaml" in rule


@pytest.mark.unit
def test_the_comparison_carries_intervals_and_the_rule() -> None:
    result = compare_runs(_run("a", NOISY_A), _run("b", NOISY_A), LIMITS)
    row = next(m for m in result.metrics if m.name == "answer_score_mean")
    assert row.baseline_interval is not None
    assert row.candidate_interval is not None
    assert "0.10" in result.noise_rule


@pytest.mark.unit
def test_without_repeat_runs_the_comparison_says_noise_is_unmeasured() -> None:
    result = compare_runs(_run("a", NOISY_A), _run("b", NOISY_A), LIMITS)
    assert any("unmeasured" in w for w in result.warnings)


@pytest.mark.unit
def test_measured_noise_wider_than_the_band_is_a_warning_not_a_change() -> None:
    baseline, repeat = _run("a", NOISY_A), _run("a2", NOISY_B)
    candidate = _run("b", NOISY_A, prompt_hashes={"investigator": "new"})
    result = compare_runs(baseline, candidate, LIMITS, history=[baseline, repeat])
    assert any("wider than" in w and "0.10" in w for w in result.warnings)
    # The gate itself is unchanged: the same verdicts as without history.
    plain = compare_runs(baseline, candidate, LIMITS)
    assert [m.verdict for m in result.metrics] == [m.verdict for m in plain.metrics]


@pytest.mark.unit
def test_measured_noise_inside_the_band_is_not_warned_about() -> None:
    baseline = _run("a", [0.80, 0.60, 0.70])
    repeat = _run("a2", [0.81, 0.60, 0.69])
    result = compare_runs(
        baseline, _run("b", [0.8, 0.6, 0.7]), LIMITS, history=[baseline, repeat]
    )
    assert not any("wider than" in w or "unmeasured" in w for w in result.warnings)


@pytest.mark.unit
@pytest.mark.anyio
async def test_the_compare_endpoint_returns_intervals_and_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_evals, "RESULTS_DIR", tmp_path)
    changed = {"investigator": "changed"}
    for run in (
        _run("eval-a", NOISY_A),
        _run("eval-b", NOISY_A, prompt_hashes=changed),
    ):
        (tmp_path / f"{run.run_id}.json").write_text(run.model_dump_json())
    transport = ASGITransport(app=api_main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/evals/compare", params={"baseline": "eval-a", "candidate": "eval-b"}
        )
    body = resp.json()
    assert resp.status_code == 200
    band = load_thresholds().gate.answer_score_noise_band
    assert f"±{band:.2f}" in body["noise_rule"]
    assert body["warnings"]
    score = next(m for m in body["metrics"] if m["name"] == "answer_score_mean")
    assert score["baseline_interval"]["low"] < score["baseline_interval"]["high"]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
