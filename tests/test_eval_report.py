"""Tests for `eval/report.py`."""

from datetime import UTC, datetime

import pytest

from recon.contracts import CaseScore, EvalRun
from recon.eval.report import render_markdown


def _case_score(**overrides: object) -> CaseScore:
    defaults: dict[str, object] = {
        "case_id": "c1",
        "task_completion": True,
        "answer_score": 0.8,
        "tool_path_exact": True,
        "tool_path_equivalent": True,
        "tool_call_accuracy": 1.0,
        "rubric_scores": {"answer_correctness": 0.8},
        "cost_eur": 0.01,
        "elapsed_ms": 100,
        "notes": "",
    }
    defaults.update(overrides)
    return CaseScore(**defaults)  # type: ignore[arg-type]


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
        "case_scores": [_case_score()],
        "aggregate": {"task_completion_rate": 1.0, "answer_score_mean": 0.8},
        "total_cost_eur": 0.01,
    }
    defaults.update(overrides)
    return EvalRun(**defaults)  # type: ignore[arg-type]


@pytest.mark.unit
def test_render_markdown_includes_run_id_and_aggregate() -> None:
    md = render_markdown(_run())

    assert "eval-1" in md
    assert "task_completion_rate" in md
    assert "c1" in md


@pytest.mark.unit
def test_render_markdown_escapes_pipe_in_notes() -> None:
    md = render_markdown(_run(case_scores=[_case_score(notes="a | b")]))

    assert "a / b" in md
