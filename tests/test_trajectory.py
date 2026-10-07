"""Tests for `eval/trajectory.py`: the per-case breakdown of the run path and
the failure class (#116). No model is called."""

from typing import Any

import pytest

from recon.contracts import AgentResult, Case, Claim, Evidence, ToolCall
from recon.eval import trajectory

pytestmark = pytest.mark.unit

CUTOFF = 0.5


def _case(expected_tool_path: list[str] | None = None) -> Case:
    return Case(
        case_id="c1",
        source="finance-agent-bench",
        question="What was revenue?",
        expected_answer="a",
        expected_tool_path=expected_tool_path,
        context={"rubric": []},
        tags=[],
        license="MIT",
        attribution="attribution",
    )


def _call(tool: str, status: str = "ok", **arguments: Any) -> ToolCall:
    return ToolCall(tool=tool, arguments=arguments, status=status, elapsed_ms=1)


def _result(
    calls: list[ToolCall], *, answer: str = "the answer", error: str | None = None
) -> AgentResult:
    return AgentResult(
        case_id="c1",
        answer=answer,
        evidence=[],
        confidence="high",
        tool_calls=calls,
        runtime="agent_sdk",
        mode="single",
        tokens_in=0,
        tokens_out=0,
        cost_eur=0.0,
        elapsed_ms=0,
        error=error,
    )


def _score(
    result: AgentResult,
    *,
    case: Case | None = None,
    rubric_scores: dict[str, float] | None = None,
    passages: trajectory.faithfulness.Passages | None = None,
    relevant: list[str] | None = None,
) -> trajectory.TrajectoryScore:
    return trajectory.trajectory_score(
        case or _case(),
        result,
        rubric_scores or {},
        passages=passages,
        relevant=relevant,
    )


# --- the breakdown --------------------------------------------------------------


def test_tool_selection_is_the_overlap_with_the_expected_tools() -> None:
    result = _result([_call("list_companies"), _call("search_filings")])

    on_path = _score(result, case=_case(["list_companies", "get_financial_fact"]))

    # {list_companies} shared out of {list_companies, search_filings, get_financial_fact}.
    assert on_path.tool_selection == pytest.approx(1 / 3)
    assert _score(result).tool_selection is None


def test_argument_correctness_counts_calls_the_tool_accepted() -> None:
    result = _result(
        [_call("get_financial_fact", "invalid_input"), _call("get_financial_fact")]
    )

    assert _score(result).argument_correctness == 0.5
    assert _score(_result([])).argument_correctness is None


def test_recovery_is_the_share_of_failed_calls_a_later_call_made_good() -> None:
    recovered = _result(
        [_call("get_financial_fact", "invalid_input"), _call("get_financial_fact")]
    )
    unrecovered = _result(
        [_call("get_financial_fact"), _call("search_filings", "unavailable")]
    )

    assert _score(recovered).recovery == 1.0
    assert _score(unrecovered).recovery == 0.0
    assert _score(_result([_call("search_filings")])).recovery is None


def test_efficiency_counts_repeated_identical_calls_against_the_run() -> None:
    result = _result(
        [
            _call("search_filings", company_id="A"),
            _call("search_filings", company_id="A"),
            _call("search_filings", company_id="B"),
            _call("search_filings", company_id="A"),
        ]
    )

    assert _score(result).efficiency == 0.5
    assert _score(_result([])).efficiency is None


def test_grounding_correctness_and_evidence_come_from_existing_scores() -> None:
    result = _result([]).model_copy(
        update={
            "claims": [Claim(text="t", importance="key", evidence_refs=["E1"])],
            "evidence_items": [
                Evidence(ref="E1", verified=True, source_type="financial_fact")
            ],
        }
    )

    score = _score(
        result,
        rubric_scores={"evidence_grounding": 0.75, "answer_correctness": 0.25},
    )

    assert score.grounding == 0.75
    assert score.final_correctness == 0.25
    assert score.evidence_sufficiency == 1.0
    assert _score(_result([])).grounding is None


def _passages(ranked: dict[str, list[str]]) -> trajectory.faithfulness.Passages:
    def passages(
        query: str, top_k: int, company_id: str | None
    ) -> list[dict[str, Any]]:
        return [{"chunk_id": chunk} for chunk in ranked[query][:top_k]]

    return passages


def test_retrieval_quality_replays_the_agents_own_searches_against_the_labels() -> None:
    result = _result(
        [
            _call("search_knowledge", query="first", top_k=2),
            _call("search_knowledge", query="second", top_k=2),
        ]
    )
    passages = _passages({"first": ["x", "a"], "second": ["a", "b"]})

    quality = _score(result, passages=passages, relevant=["a", "b", "c"])
    assert quality.retrieval_quality is not None
    retrieved = quality.retrieval_quality

    # First-seen order across the searches: x, a, b.
    assert retrieved.retrieved_count == 3
    assert retrieved.recall == pytest.approx(2 / 3)
    assert retrieved.precision_at_5 == pytest.approx(2 / 5)
    assert retrieved.mrr_at_5 == 0.5
    assert 0.0 < retrieved.ndcg_at_5 < 1.0


def test_retrieval_quality_is_none_without_labels_or_a_replay() -> None:
    result = _result([_call("search_knowledge", query="first")])
    passages = _passages({"first": ["a"]})

    assert _score(result, passages=passages).retrieval_quality is None
    assert _score(result, relevant=["a"]).retrieval_quality is None


def test_a_labelled_case_that_never_searched_retrieved_nothing() -> None:
    quality = _score(_result([]), passages=_passages({}), relevant=["a"])

    assert quality.retrieval_quality is not None
    assert quality.retrieval_quality.recall == 0.0
    assert quality.retrieval_quality.retrieved_count == 0


# --- the failure class -----------------------------------------------------------


def _classify(
    result: AgentResult,
    *,
    answer_score: float = 0.0,
    judge_failed: bool = False,
    cutoff: float | None = CUTOFF,
    score: trajectory.TrajectoryScore | None = None,
) -> tuple[str | None, str | None]:
    return trajectory.classify_failure(
        result,
        score or _score(result),
        answer_score=answer_score,
        judge_failed=judge_failed,
        correct_answer_score=cutoff,
    )


def test_a_correct_answer_has_no_failure() -> None:
    assert _classify(_result([_call("search_filings")]), answer_score=0.8) == (
        "none",
        None,
    )


def test_a_budget_breach_is_budget_even_with_an_answer() -> None:
    error = "token budget of 60000 exceeded (61000 used) - reported after the fact"
    failure, reason = _classify(_result([], error=error), answer_score=0.9)

    assert failure == "budget"
    assert reason == error


def test_an_error_without_an_answer_is_a_runtime_error() -> None:
    failure, reason = _classify(_result([], answer="", error="CLI crashed"))

    assert failure == "runtime_error"
    assert reason == "CLI crashed"


def test_unscored_cases_have_no_class() -> None:
    result = _result([_call("search_filings")])

    assert _classify(result, judge_failed=True) == (
        None,
        "the judge couldn't score this case",
    )
    assert _classify(result, cutoff=None) == (
        None,
        "no correct_answer_score cutoff is set",
    )


def test_an_unrecovered_tool_failure_is_tool_use() -> None:
    failure, reason = _classify(_result([_call("get_financial_fact", "invalid_input")]))

    assert failure == "tool_use"
    assert reason == "1 failed tool call(s) never succeeded on retry"


def test_answering_without_any_tool_call_is_tool_use() -> None:
    assert _classify(_result([])) == ("tool_use", "answered without calling a tool")


def test_no_labelled_passage_retrieved_is_retrieval() -> None:
    result = _result([_call("search_knowledge", query="q")])
    score = _score(result, passages=_passages({"q": ["x"]}), relevant=["a"])

    assert _classify(result, score=score) == (
        "retrieval",
        "none of the labelled passages were retrieved",
    )


def test_lookups_that_all_came_back_empty_are_retrieval() -> None:
    result = _result(
        [_call("search_filings", "empty"), _call("search_knowledge", "empty")]
    )

    assert _classify(result) == ("retrieval", "every lookup came back empty")


def test_a_wrong_answer_from_usable_results_is_reasoning() -> None:
    failure, reason = _classify(
        _result([_call("get_financial_fact")]), answer_score=0.3
    )

    assert failure == "reasoning"
    assert reason == "the tools returned data, but the answer scored 0.30, below 0.5"
