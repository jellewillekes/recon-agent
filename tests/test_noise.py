"""Tests for `eval/noise.py`: run-to-run noise measured from stored repeat
runs, which is what the gate's band is about (ADR 0028, 0036)."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from recon.contracts import CaseScore, EvalRun
from recon.eval.noise import estimate_noise, repeat_groups

pytestmark = pytest.mark.unit

RESULTS = Path(__file__).parent.parent / "evals" / "results"


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
        "aggregate": {},
        "total_cost_eur": 0.3,
        "tool_data_snapshot": "snap",
    }
    fields.update(overrides)
    return EvalRun(**fields)


def test_runs_with_the_same_settings_form_a_group() -> None:
    a, b = _run("a", [0.8, 0.6, 0.4]), _run("b", [0.9, 0.5, 0.4])
    assert repeat_groups([a, b]) == [[a, b]]


@pytest.mark.parametrize(
    "different",
    [
        {"prompt_hashes": {"investigator": "other"}},
        {"model_config_hash": "other"},
        {"rubric_version": "5"},
        {"tool_data_snapshot": "other"},
        {"mode": "multi"},
        {"routing": True},
    ],
)
def test_a_changed_setting_breaks_the_group(different: dict[str, Any]) -> None:
    a = _run("a", [0.8, 0.6, 0.4])
    b = _run("b", [0.9, 0.5, 0.4], **different)
    assert repeat_groups([a, b]) == []


def test_a_different_case_set_breaks_the_group() -> None:
    assert repeat_groups([_run("a", [0.8, 0.6]), _run("b", [0.8, 0.6, 0.4])]) == []


def test_a_single_run_is_not_a_repeat() -> None:
    assert repeat_groups([_run("a", [0.8, 0.6])]) == []


def test_noise_matches_a_hand_calculation() -> None:
    """Per-case differences 0.1, -0.1, 0. Per-case run-to-run variance is
    d^2 / 2: 0.005, 0.005, 0, mean 1/300. The sd of a difference between two
    run means is sqrt(2 * (1/300) / 3) = 0.04714. With 3 cases and 2 runs the
    degrees of freedom are 3 and t = 3.182, so the 95% band is 0.150."""
    noise = estimate_noise([_run("a", [0.8, 0.6, 0.4]), _run("b", [0.9, 0.5, 0.4])])
    assert noise.runs == 2
    assert noise.cases == 3
    assert noise.sd_of_difference == pytest.approx(0.04714, abs=1e-4)
    assert noise.band_95 == pytest.approx(0.150, abs=1e-3)


def test_more_repeats_use_every_run() -> None:
    two = estimate_noise([_run("a", [0.8, 0.6, 0.4]), _run("b", [0.9, 0.5, 0.4])])
    three = estimate_noise(
        [
            _run("a", [0.8, 0.6, 0.4]),
            _run("b", [0.9, 0.5, 0.4]),
            _run("c", [0.7, 0.7, 0.4]),
        ]
    )
    assert three.runs == 3
    assert three.band_95 != two.band_95


def test_noise_needs_two_runs() -> None:
    with pytest.raises(ValueError, match="two"):
        estimate_noise([_run("a", [0.8, 0.6])])


def test_the_stored_repeat_pair_from_the_noise_study() -> None:
    """eval-20261006T204756Z and eval-20261006T210553Z are the pair measured
    in docs/eval-noise.md (#77). Their per-case differences are 0.0, 0.0, 0.1,
    0.07, 0.1, 0.47 and -0.43, so the sd of the difference of means is about
    0.094 and the 95% band about 0.22."""
    runs = [
        EvalRun.model_validate(json.loads((RESULTS / f"{name}.json").read_text()))
        for name in ("eval-20261006T204756Z", "eval-20261006T210553Z")
    ]
    assert repeat_groups(runs) == [runs]
    noise = estimate_noise(runs)
    assert noise.cases == 7
    assert noise.sd_of_difference == pytest.approx(0.094, abs=0.002)
    assert noise.band_95 == pytest.approx(0.223, abs=0.01)
