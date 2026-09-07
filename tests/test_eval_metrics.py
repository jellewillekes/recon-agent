"""Tests for `eval/metrics.py` — pure, no-LLM per-case metrics."""

import pytest

from recon.contracts import AgentResult, Case, ToolCall
from recon.eval import metrics

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
