"""Tests for `eval/metrics.py` — pure, no-LLM per-case metrics."""

from typing import get_args

import pytest

from recon.contracts import AgentResult, Case, ReviewFlagResult, ToolCall, ToolResult
from recon.eval import metrics
from recon.eval.rubrics import Rubric

UNUSABLE_STATUSES = frozenset({"invalid_input", "unavailable"})

CASE_NO_EXPECTED_PATH = Case(
    case_id="finance-agent-bench:abc123",
    source="finance-agent-bench",
    question="q",
    expected_answer="a",
    expected_tool_path=None,
    context={},
    tags=["Trends"],
    license="MIT",
    attribution="attribution",
)

CASE_WITH_EXPECTED_PATH = Case(
    case_id="synthetic:1",
    source="synthetic",
    question="q",
    expected_answer="a",
    expected_tool_path=["list_companies", "get_financial_fact"],
    context={},
    tags=["Trends"],
    license="MIT",
    attribution="attribution",
)


def _agent_result(**overrides: object) -> AgentResult:
    defaults: dict[str, object] = {
        "case_id": "case-1",
        "answer": "the answer",
        "evidence": ["ev"],
        "confidence": "high",
        "tool_calls": [],
        "runtime": "agent_sdk",
        "mode": "single",
        "tokens_in": 10,
        "tokens_out": 5,
        "cost_eur": 0.01,
        "elapsed_ms": 100,
        "error": None,
    }
    defaults.update(overrides)
    return AgentResult(**defaults)  # type: ignore[arg-type]


def _tool_call(tool: str, status: str) -> ToolCall:
    return ToolCall(tool=tool, arguments={}, status=status, elapsed_ms=1)


@pytest.mark.unit
def test_task_completion_true_when_answered_without_error() -> None:
    assert metrics.task_completion(_agent_result()) is True


@pytest.mark.unit
def test_task_completion_false_on_error() -> None:
    result = _agent_result(error="boom", answer="")
    assert metrics.task_completion(result) is False


@pytest.mark.unit
def test_task_completion_false_on_empty_answer() -> None:
    result = _agent_result(answer="   ")
    assert metrics.task_completion(result) is False


@pytest.mark.unit
def test_tool_path_exact_na_when_no_expected_path() -> None:
    exact, note = metrics.tool_path_exact(CASE_NO_EXPECTED_PATH, _agent_result())
    assert exact is True
    assert note == metrics.TOOL_PATH_NA_NOTE


@pytest.mark.unit
def test_tool_path_equivalent_na_when_no_expected_path() -> None:
    equivalent, note = metrics.tool_path_equivalent(
        CASE_NO_EXPECTED_PATH, _agent_result()
    )
    assert equivalent is True
    assert note == metrics.TOOL_PATH_NA_NOTE


@pytest.mark.unit
def test_tool_path_exact_matches_order() -> None:
    result = _agent_result(
        tool_calls=[
            _tool_call("list_companies", "ok"),
            _tool_call("get_financial_fact", "ok"),
        ]
    )
    exact, note = metrics.tool_path_exact(CASE_WITH_EXPECTED_PATH, result)
    assert exact is True
    assert note is None


@pytest.mark.unit
def test_tool_path_exact_fails_on_wrong_order() -> None:
    result = _agent_result(
        tool_calls=[
            _tool_call("get_financial_fact", "ok"),
            _tool_call("list_companies", "ok"),
        ]
    )
    exact, _ = metrics.tool_path_exact(CASE_WITH_EXPECTED_PATH, result)
    assert exact is False


@pytest.mark.unit
def test_tool_path_equivalent_ignores_order() -> None:
    result = _agent_result(
        tool_calls=[
            _tool_call("get_financial_fact", "ok"),
            _tool_call("list_companies", "ok"),
        ]
    )
    equivalent, _ = metrics.tool_path_equivalent(CASE_WITH_EXPECTED_PATH, result)
    assert equivalent is True


@pytest.mark.unit
def test_tool_call_accuracy_no_calls_is_vacuous_pass() -> None:
    assert metrics.tool_call_accuracy(_agent_result()) == 1.0


@pytest.mark.unit
def test_tool_call_accuracy_empty_status_counts_as_usable() -> None:
    result = _agent_result(tool_calls=[_tool_call("search_filings", "empty")])
    assert metrics.tool_call_accuracy(result) == 1.0


@pytest.mark.unit
def test_tool_call_accuracy_mixed_statuses() -> None:
    result = _agent_result(
        tool_calls=[
            _tool_call("a", "ok"),
            _tool_call("b", "invalid_input"),
            _tool_call("c", "unavailable"),
            _tool_call("d", "truncated"),
        ]
    )
    assert metrics.tool_call_accuracy(result) == 0.5


@pytest.mark.unit
@pytest.mark.parametrize(
    "status", ["would_write", "confirmation_required", "created", "already_exists"]
)
def test_tool_call_accuracy_write_statuses_count_as_usable(status: str) -> None:
    result = _agent_result(tool_calls=[_tool_call("flag_case_for_review", status)])
    assert metrics.tool_call_accuracy(result) == 1.0


@pytest.mark.unit
def test_tool_call_accuracy_full_flag_protocol_scores_perfect() -> None:
    # The sequence langgraph_multi._confirm_flag_node records on an approved flag.
    result = _agent_result(
        tool_calls=[
            _tool_call("get_financial_fact", "ok"),
            _tool_call("flag_case_for_review", "would_write"),
            _tool_call("flag_case_for_review", "confirmation_required"),
            _tool_call("flag_case_for_review", "created"),
        ]
    )
    assert metrics.tool_call_accuracy(result) == 1.0


@pytest.mark.unit
def test_tool_call_accuracy_unknown_status_counts_against() -> None:
    result = _agent_result(
        tool_calls=[
            _tool_call("flag_case_for_review", "created"),
            _tool_call("flag_case_for_review", "unknown"),
        ]
    )
    assert metrics.tool_call_accuracy(result) == 0.5


@pytest.mark.unit
def test_every_contract_status_is_classified() -> None:
    contract_statuses = set(
        get_args(ToolResult.model_fields["status"].annotation)
    ) | set(get_args(ReviewFlagResult.model_fields["status"].annotation))
    classified = metrics.USABLE_STATUSES | UNUSABLE_STATUSES
    assert contract_statuses == classified, (
        "A ToolResult or ReviewFlagResult status is missing from "
        "metrics.USABLE_STATUSES / UNUSABLE_STATUSES. Decide whether it is usable "
        "and add it to one of them."
    )


def _rubrics(**weights: float) -> dict[str, Rubric]:
    return {
        dimension: Rubric(dimension=dimension, version=1, weight=weight, assertions=[])
        for dimension, weight in weights.items()
    }


REPO_WEIGHTS = _rubrics(
    answer_correctness=0.5, evidence_grounding=0.3, tool_efficiency=0.2
)


@pytest.mark.unit
def test_weighted_answer_score_uses_rubric_weights() -> None:
    # The committed eval-20260907T102452Z's dimension means, re-weighted.
    scores = {
        "answer_correctness": 0.181,
        "evidence_grounding": 1.0,
        "tool_efficiency": 0.833,
    }
    assert metrics.weighted_answer_score(scores, REPO_WEIGHTS) == pytest.approx(
        0.5 * 0.181 + 0.3 * 1.0 + 0.2 * 0.833
    )


@pytest.mark.unit
def test_weighted_answer_score_renormalizes_over_scored_dimensions() -> None:
    scores = {"answer_correctness": 1.0, "tool_efficiency": 0.0}
    assert metrics.weighted_answer_score(scores, REPO_WEIGHTS) == pytest.approx(
        0.5 / 0.7
    )


@pytest.mark.unit
@pytest.mark.parametrize("value", [0.0, 1.0])
def test_weighted_answer_score_uniform_scores(value: float) -> None:
    scores = dict.fromkeys(REPO_WEIGHTS, value)
    assert metrics.weighted_answer_score(scores, REPO_WEIGHTS) == pytest.approx(value)


@pytest.mark.unit
def test_weighted_answer_score_ignores_unknown_dimensions() -> None:
    scores = {"answer_correctness": 0.4, "not_a_rubric": 1.0}
    assert metrics.weighted_answer_score(scores, REPO_WEIGHTS) == pytest.approx(0.4)


@pytest.mark.unit
def test_weighted_answer_score_zero_when_nothing_weighted() -> None:
    assert metrics.weighted_answer_score({}, REPO_WEIGHTS) == 0.0
