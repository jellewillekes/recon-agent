"""`config/thresholds.yaml`, the cost-per-correct-answer metric (#77) and
`scripts/check_baseline.py`, the CI check on the committed baseline (#16)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from recon.contracts import CaseScore, EvalRun
from recon.eval.gate import SKIPPED_AT_COST_CAP, check_gate
from recon.eval.harness import with_cost_per_correct_answer
from recon.eval.metrics import cost_per_correct_answer_eur
from recon.eval.thresholds import GateThresholds, load_thresholds

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_baseline.py"
RESULT = ROOT / "evals" / "results" / "eval-20260907T102452Z.json"

pytestmark = pytest.mark.unit


def _score(case_id: str, answer_score: float, cost_eur: float) -> CaseScore:
    return CaseScore(
        case_id=case_id,
        task_completion=True,
        answer_score=answer_score,
        tool_path_exact=True,
        tool_path_equivalent=True,
        tool_call_accuracy=1.0,
        rubric_scores={},
        cost_eur=cost_eur,
        elapsed_ms=1,
        notes="",
    )


def _run(**overrides: object) -> EvalRun:
    data = json.loads(RESULT.read_text(encoding="utf-8"))
    data.update({"tool_data_snapshot": "fixture-abc", **overrides})
    return EvalRun.model_validate(data)


def test_the_repo_thresholds_keep_the_gate_limits() -> None:
    """Moved unchanged from gate.py. Lowering one is the user's call (AGENTS.md)."""
    thresholds = load_thresholds(ROOT / "config" / "thresholds.yaml")
    assert thresholds.gate == GateThresholds(
        answer_score_max_relative_drop=0.02, cost_max_relative_rise=0.20
    )


def test_a_misspelled_key_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "thresholds.yaml"
    path.write_text(
        "gate: {answer_score_max_relative_drop: 0.02, cost_max_relative_rise: 0.2}\n"
        "baseline_minimum: {answer_score_mean: 0.5}\n"
    )
    with pytest.raises(ValidationError, match="baseline_minimum"):
        load_thresholds(path)


def test_the_gate_uses_the_limits_it_is_given() -> None:
    baseline = _run(aggregate={"task_completion_rate": 1.0, "answer_score_mean": 0.5})
    candidate = _run(aggregate={"task_completion_rate": 1.0, "answer_score_mean": 0.45})
    loose = GateThresholds(answer_score_max_relative_drop=0.2, cost_max_relative_rise=1)
    strict = GateThresholds(
        answer_score_max_relative_drop=0.02, cost_max_relative_rise=1
    )
    assert check_gate(candidate, baseline, loose) == []
    assert "dropped more than 2%" in check_gate(candidate, baseline, strict)[0]


def test_cost_per_correct_answer_counts_cases_at_the_cutoff() -> None:
    scores = [_score("a", 1.0, 0.10), _score("b", 0.5, 0.10), _score("c", 0.2, 0.10)]
    assert cost_per_correct_answer_eur(scores, 0.5) == pytest.approx(0.15)
    assert cost_per_correct_answer_eur(scores, 1.0) == pytest.approx(0.30)
    assert cost_per_correct_answer_eur(scores, 1.01) is None


def test_the_metric_is_added_only_with_a_cutoff() -> None:
    run = _run()
    assert with_cost_per_correct_answer(run, None) is run
    added = with_cost_per_correct_answer(run, 0.0)
    expected = run.total_cost_eur / len(run.case_scores)
    assert added.aggregate["cost_per_correct_answer_eur"] == pytest.approx(expected)
    assert "cost_per_correct_answer_eur" not in run.aggregate


def _check(tmp_path: Path, baseline: EvalRun | None, minimums: str = "{}") -> str:
    thresholds = tmp_path / "thresholds.yaml"
    thresholds.write_text(
        "gate: {answer_score_max_relative_drop: 0.02, cost_max_relative_rise: 0.2}\n"
        f"correct_answer_score: null\nbaseline_minimums: {minimums}\n"
    )
    path = tmp_path / "baseline.json"
    if baseline is not None:
        path.write_text(baseline.model_dump_json())
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--baseline",
            str(path),
            "--thresholds",
            str(thresholds),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return f"exit={result.returncode}\n{result.stdout}"


def test_no_baseline_yet_passes_with_a_notice(tmp_path: Path) -> None:
    assert _check(tmp_path, None).startswith("exit=0\n")


def test_a_usable_baseline_passes(tmp_path: Path) -> None:
    out = _check(tmp_path, _run(), "{answer_score_mean: 0.1}")
    assert out.startswith("exit=0\n")
    assert "1 minimum(s) checked" in out


@pytest.mark.parametrize(
    ("overrides", "minimums", "problem"),
    [
        ({"tool_data_snapshot": None}, "{}", "doesn't record which tool data"),
        (
            {"aggregate": {"answer_score_mean": 0.9, SKIPPED_AT_COST_CAP: 2.0}},
            "{}",
            "stopped at its cost cap",
        ),
        ({}, "{answer_score_mean: 0.9}", "below the minimum 0.9"),
        ({}, "{no_such_metric: 0.1}", "no_such_metric is missing"),
    ],
    ids=["no-snapshot", "capped", "below-minimum", "missing-metric"],
)
def test_an_unusable_baseline_fails(
    tmp_path: Path, overrides: dict[str, object], minimums: str, problem: str
) -> None:
    out = _check(tmp_path, _run(**overrides), minimums)
    assert out.startswith("exit=1\n")
    assert problem in out


def test_a_malformed_baseline_fails(tmp_path: Path) -> None:
    (tmp_path / "baseline.json").write_text('{"run_id": "x"}')
    thresholds = ROOT / "config" / "thresholds.yaml"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--baseline",
            str(tmp_path / "baseline.json"),
            "--thresholds",
            str(thresholds),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "isn't a valid EvalRun" in result.stdout
