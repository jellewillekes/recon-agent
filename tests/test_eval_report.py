"""Tests for `eval/report.py`."""

from datetime import UTC, datetime

import pytest

from recon.contracts import CaseScore, ClaimVerification, EvalRun
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


@pytest.mark.unit
def test_render_markdown_shows_the_tool_data_snapshot() -> None:
    assert "- Tool data: 20260928" in render_markdown(
        _run(tool_data_snapshot="20260928")
    )
    assert "- Tool data: not recorded" in render_markdown(_run())


@pytest.mark.unit
def test_render_markdown_lists_each_cases_tools() -> None:
    md = render_markdown(
        _run(
            case_scores=[
                _case_score(tool_names=["search_knowledge", "get_financial_fact"])
            ]
        )
    )
    assert "| tools |" in md
    assert "search_knowledge, get_financial_fact" in md


@pytest.mark.unit
def test_a_result_recorded_before_tool_names_still_loads() -> None:
    raw = _case_score().model_dump()
    del raw["tool_names"]
    assert CaseScore.model_validate(raw).tool_names == []


@pytest.mark.unit
def test_render_markdown_flags_a_run_that_didnt_score_every_case() -> None:
    """Review of #123: the placeholder 0.0 mustn't read as a measured score."""
    run = _run(
        aggregate={"answer_score_mean": 0.4, "cases_judge_failed": 1.0},
        case_scores=[_case_score(judge_failed=True, answer_score=0.0)],
    )

    markdown = render_markdown(run)

    assert "Not comparable: has 1 case(s) the judge couldn't score" in markdown
    assert "| unscored |" in markdown


@pytest.mark.unit
def test_render_markdown_lists_each_cases_failure_class() -> None:
    run = _run(case_scores=[_case_score(failure_class="tool_use")])

    assert "| tool_use |" in render_markdown(run)


def _verification(verdict: str, text: str = "claim") -> ClaimVerification:
    return ClaimVerification(
        claim_id="claim-1",
        text=text,
        verdict=verdict,  # type: ignore[arg-type]
        evidence_refs=["E1"],
        claimed_value=None,
        recomputed_value=None,
        tolerance=None,
        reasoning="r",
    )


@pytest.mark.unit
def test_render_markdown_counts_claim_verdicts_and_quotes_contradictions() -> None:
    """#139: the summary shows how the run's claims were verified."""
    cases = [
        _case_score(
            case_id="c1",
            verifications=[
                _verification("SUPPORTED"),
                _verification("CONTRADICTED", "Revenue was $600 million"),
            ],
        ),
        _case_score(case_id="c2", verifications=[]),
    ]

    md = render_markdown(_run(case_scores=cases, verifier_version="1:tol=0"))

    assert "## Claims" in md
    assert "Verifier: 1:tol=0" in md
    assert "| SUPPORTED | 1 |" in md
    assert "| CONTRADICTED | 1 |" in md
    assert "| UNSUPPORTED | 0 |" in md
    assert "50.0% of the claims checked" in md
    assert '- c1: "Revenue was $600 million"' in md


@pytest.mark.unit
def test_render_markdown_says_when_claims_were_not_verified() -> None:
    md = render_markdown(_run())

    assert "## Claims" in md
    assert "Not verified in this run." in md
