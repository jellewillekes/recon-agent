"""Tests for `runtimes/agent_sdk.py`.

Monkeypatches `claude_agent_sdk.query` with a fabricated message stream — no
real model call, no network, no cost — and asserts the parsing: tool-call
ordering and status/elapsed_ms extraction, cost conversion, and that any
failure surfaces as `AgentResult.error` rather than propagating, per the
`Runtime` protocol contract.
"""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from recon.contracts import Case
from recon.runtimes import agent_sdk

CASE = Case(
    case_id="finance-agent-bench:abc123",
    source="finance-agent-bench",
    question="What sector is FIRM-001 in?",
    expected_answer="Industrials",
    expected_tool_path=None,
    context={
        "question_type": "Simple Lookups",
        "expert_time_minutes": 1.0,
        "rubric": {},
    },
    tags=["Simple Lookups"],
    license="MIT",
    attribution="test fixture",
)


def _tool_result_message(tool_use_id: str, status: str, elapsed_ms: int) -> UserMessage:
    payload = {
        "status": status,
        "data": [{"company_id": "FIRM-001"}] if status == "ok" else [],
        "row_count": 1 if status == "ok" else 0,
        "message": "ok",
        "elapsed_ms": elapsed_ms,
    }
    return UserMessage(
        content=[ToolResultBlock(tool_use_id=tool_use_id, content=json.dumps(payload))]
    )


def _result_message(**overrides: Any) -> ResultMessage:
    defaults: dict[str, Any] = {
        "subtype": "success",
        "duration_ms": 500,
        "duration_api_ms": 400,
        "is_error": False,
        "num_turns": 2,
        "session_id": "session-1",
        "total_cost_usd": 0.01,
        "usage": {"input_tokens": 100, "output_tokens": 50},
        "structured_output": {
            "answer": "Industrials",
            "evidence": ["FIRM-001"],
            "confidence": "high",
        },
    }
    defaults.update(overrides)
    return ResultMessage(**defaults)


def _patch_query(monkeypatch: pytest.MonkeyPatch, messages: list[object]) -> None:
    async def fake_query(
        *, prompt: str, options: ClaudeAgentOptions | None = None
    ) -> AsyncIterator[object]:
        for message in messages:
            yield message

    monkeypatch.setattr(agent_sdk, "query", fake_query)


def _patch_options(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_build_options(
        models_config_path: Path, prompt_path: Path
    ) -> tuple[ClaudeAgentOptions, float]:
        return ClaudeAgentOptions(), 0.9

    monkeypatch.setattr(agent_sdk, "_build_options", fake_build_options)


@pytest.mark.unit
def test_run_success_parses_tool_calls_and_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_options(monkeypatch)
    _patch_query(
        monkeypatch,
        [
            AssistantMessage(
                content=[
                    ToolUseBlock(
                        id="tu1",
                        name="mcp__recon-tools__list_companies_tool",
                        input={"sector": None},
                    )
                ],
                model="claude-sonnet-5",
            ),
            _tool_result_message("tu1", status="ok", elapsed_ms=12),
            _result_message(),
        ],
    )

    result = agent_sdk.AgentSdkRuntime().run(CASE)

    assert result.error is None
    assert result.answer == "Industrials"
    assert result.evidence == ["FIRM-001"]
    assert result.confidence == "high"
    assert result.runtime == "agent_sdk"
    assert result.mode == "single"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].tool == "list_companies"
    assert result.tool_calls[0].arguments == {"sector": None}
    assert result.tool_calls[0].status == "ok"
    assert result.tool_calls[0].elapsed_ms == 12
    assert result.tokens_in == 100
    assert result.tokens_out == 50
    assert result.cost_eur == pytest.approx(0.01 * 0.9)


@pytest.mark.unit
def test_run_preserves_tool_call_order(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_options(monkeypatch)
    _patch_query(
        monkeypatch,
        [
            AssistantMessage(
                content=[
                    ToolUseBlock(
                        id="tu1",
                        name="mcp__recon-tools__list_companies_tool",
                        input={"sector": None},
                    )
                ],
                model="claude-sonnet-5",
            ),
            _tool_result_message("tu1", status="ok", elapsed_ms=5),
            AssistantMessage(
                content=[
                    ToolUseBlock(
                        id="tu2",
                        name="mcp__recon-tools__get_financial_fact_tool",
                        input={"company_id": "FIRM-001", "concept": "revenue"},
                    )
                ],
                model="claude-sonnet-5",
            ),
            _tool_result_message("tu2", status="empty", elapsed_ms=7),
            _result_message(),
        ],
    )

    result = agent_sdk.AgentSdkRuntime().run(CASE)

    assert result.error is None
    assert [call.tool for call in result.tool_calls] == [
        "list_companies",
        "get_financial_fact",
    ]
    assert [call.status for call in result.tool_calls] == ["ok", "empty"]


@pytest.mark.unit
def test_run_no_result_message_populates_error_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_options(monkeypatch)
    _patch_query(monkeypatch, [])

    result = agent_sdk.AgentSdkRuntime().run(CASE)

    assert result.error is not None
    assert "ResultMessage" in result.error
    assert result.answer == ""
    assert result.evidence == []
    assert result.confidence == "low"
    assert result.tool_calls == []
    assert result.cost_eur == 0.0


@pytest.mark.unit
def test_run_sdk_error_populates_error_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_options(monkeypatch)
    _patch_query(
        monkeypatch,
        [
            _result_message(
                is_error=True, subtype="error_during_execution", errors=["boom"]
            )
        ],
    )

    result = agent_sdk.AgentSdkRuntime().run(CASE)

    assert result.error is not None
    assert "boom" in result.error


@pytest.mark.unit
def test_run_missing_structured_output_populates_error_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_options(monkeypatch)
    _patch_query(monkeypatch, [_result_message(structured_output=None)])

    result = agent_sdk.AgentSdkRuntime().run(CASE)

    assert result.error is not None
    assert result.answer == ""


@pytest.mark.unit
def test_run_options_failure_populates_error_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_build_options(
        models_config_path: Path, prompt_path: Path
    ) -> tuple[ClaudeAgentOptions, float]:
        raise FileNotFoundError("config/models.yaml not found")

    monkeypatch.setattr(agent_sdk, "_build_options", fake_build_options)

    result = agent_sdk.AgentSdkRuntime().run(CASE)

    assert result.error is not None
    assert "models.yaml" in result.error
