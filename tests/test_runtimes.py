"""Tests for `runtimes/agent_sdk.py`.

Monkeypatches `claude_agent_sdk.query` with a fabricated message stream — no
real model call, no network, no cost — and asserts the parsing: tool-call
ordering and status/elapsed_ms extraction, cost conversion, and that any
failure surfaces as `AgentResult.error` rather than propagating, per the
`Runtime` protocol contract. Also covers `run_async` directly, including
that cancelling it actually reaches `query()`'s own cleanup (the property
`run()` + `asyncio.to_thread` couldn't provide — see
`docs/adr/0006-api-timeout-cancellation-fixed.md`).
"""

import asyncio
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


def _generous_budget() -> agent_sdk.RunBudget:
    """High enough that no fake message stream in this file trips it by
    accident — budget-breach behavior gets its own dedicated tests below.
    """
    return agent_sdk.RunBudget(
        max_tool_calls=1000, max_tokens=10_000_000, max_wall_clock_s=3600.0
    )


def _patch_options(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_build_options(
        models_config_path: Path, prompt_path: Path
    ) -> tuple[ClaudeAgentOptions, float, agent_sdk.RunBudget]:
        return ClaudeAgentOptions(), 0.9, _generous_budget()

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
def test_run_parses_list_shaped_tool_result_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ToolResultBlock.content` types as `str | list[dict[str, Any]] | None` —
    real MCP round trips observed so far always send the plain-string form,
    but the list-of-text-block form is part of the SDK's own declared type
    and must parse the same way.
    """
    _patch_options(monkeypatch)
    payload = {
        "status": "ok",
        "data": [{"company_id": "FIRM-001"}],
        "row_count": 1,
        "message": "ok",
        "elapsed_ms": 9,
    }
    list_shaped_result = UserMessage(
        content=[
            ToolResultBlock(
                tool_use_id="tu1",
                content=[{"type": "text", "text": json.dumps(payload)}],
            )
        ]
    )
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
            list_shaped_result,
            _result_message(),
        ],
    )

    result = agent_sdk.AgentSdkRuntime().run(CASE)

    assert result.error is None
    assert result.tool_calls[0].status == "ok"
    assert result.tool_calls[0].elapsed_ms == 9


@pytest.mark.unit
def test_run_excludes_read_from_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """`Read` is kept only for the CLI's own oversized-result recovery — it
    isn't one of tools/server.py's tools and its content doesn't match
    ToolResult's shape, so it must never end up in `AgentResult.tool_calls`.
    """
    _patch_options(monkeypatch)
    _patch_query(
        monkeypatch,
        [
            AssistantMessage(
                content=[
                    ToolUseBlock(
                        id="tu1",
                        name="Read",
                        input={"file_path": "/tmp/offloaded-result.txt"},
                    )
                ],
                model="claude-sonnet-5",
            ),
            UserMessage(
                content=[
                    ToolResultBlock(tool_use_id="tu1", content="file contents here")
                ]
            ),
            _result_message(),
        ],
    )

    result = agent_sdk.AgentSdkRuntime().run(CASE)

    assert result.error is None
    assert result.tool_calls == []


@pytest.mark.unit
def test_run_parses_status_from_result_without_elapsed_ms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ReviewFlagResult` (a write, not a timed read) has no `elapsed_ms`
    field — a real `status` like "confirmation_required" must still come
    through, not fall back to the generic "unknown".
    """
    _patch_options(monkeypatch)
    _patch_query(
        monkeypatch,
        [
            AssistantMessage(
                content=[
                    ToolUseBlock(
                        id="tu1",
                        name="mcp__recon-tools__flag_case_for_review_tool",
                        input={"case_id": "case-001", "dry_run": False},
                    )
                ],
                model="claude-sonnet-5",
            ),
            UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id="tu1",
                        content=json.dumps(
                            {
                                "status": "confirmation_required",
                                "flag": None,
                                "message": "Call again with confirmed=True.",
                            }
                        ),
                    )
                ]
            ),
            _result_message(),
        ],
    )

    result = agent_sdk.AgentSdkRuntime().run(CASE)

    assert result.error is None
    assert result.tool_calls[0].tool == "flag_case_for_review"
    assert result.tool_calls[0].status == "confirmation_required"
    assert result.tool_calls[0].elapsed_ms == 0


def _offload_notice(path: Path) -> str:
    return (
        f"Error: result (53,048 characters) exceeds maximum allowed tokens. "
        f"Output has been saved to {path}.\n"
        "Format: JSON with schema: {status: string, data: [{...}], "
        "row_count: number, message: string, elapsed_ms: number}\n"
    )


@pytest.mark.unit
def test_run_recovers_status_from_offloaded_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When a result is too large to inline, the CLI swaps the tool's own
    ToolResult JSON for an offload notice naming a file with that JSON. The
    call still succeeded — real status must be recovered from the file, not
    reported as the generic fallback.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    tool_results_dir = tmp_path / ".claude" / "projects" / "p1" / "s1" / "tool-results"
    tool_results_dir.mkdir(parents=True)
    offloaded_file = tool_results_dir / "mcp-recon-tools-list_companies_tool-1.txt"
    offloaded_file.write_text(
        json.dumps(
            {
                "status": "truncated",
                "data": [{"company_id": "FIRM-001"}],
                "row_count": 500,
                "message": "500 companies match, showing the first 500.",
                "elapsed_ms": 42,
            }
        )
    )
    _patch_options(monkeypatch)
    _patch_query(
        monkeypatch,
        [
            AssistantMessage(
                content=[
                    ToolUseBlock(
                        id="tu1",
                        name="mcp__recon-tools__list_companies_tool",
                        input={},
                    )
                ],
                model="claude-sonnet-5",
            ),
            UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id="tu1", content=_offload_notice(offloaded_file)
                    )
                ]
            ),
            _result_message(),
        ],
    )

    result = agent_sdk.AgentSdkRuntime().run(CASE)

    assert result.error is None
    assert result.tool_calls[0].status == "truncated"
    assert result.tool_calls[0].elapsed_ms == 42


@pytest.mark.unit
def test_run_refuses_offload_notice_outside_trusted_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A tool result is untrusted input in principle (step 3's search_filings
    will eventually source from real documents) — an offload notice pointing
    outside the CLI's own project directory must never be followed, even if a
    file happens to exist there.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    outside_file = tmp_path / "mcp-recon-tools-list_companies_tool-1.txt"
    outside_file.write_text(json.dumps({"status": "ok", "elapsed_ms": 1}))
    _patch_options(monkeypatch)
    _patch_query(
        monkeypatch,
        [
            AssistantMessage(
                content=[
                    ToolUseBlock(
                        id="tu1",
                        name="mcp__recon-tools__list_companies_tool",
                        input={},
                    )
                ],
                model="claude-sonnet-5",
            ),
            UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id="tu1", content=_offload_notice(outside_file)
                    )
                ]
            ),
            _result_message(),
        ],
    )

    result = agent_sdk.AgentSdkRuntime().run(CASE)

    assert result.error is None
    assert result.tool_calls[0].status == "unknown"
    assert result.tool_calls[0].elapsed_ms == 0


@pytest.mark.unit
def test_run_offload_notice_missing_file_falls_back_to_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    tool_results_dir = tmp_path / ".claude" / "projects" / "p1" / "s1" / "tool-results"
    tool_results_dir.mkdir(parents=True)
    missing_file = tool_results_dir / "mcp-recon-tools-list_companies_tool-1.txt"
    _patch_options(monkeypatch)
    _patch_query(
        monkeypatch,
        [
            AssistantMessage(
                content=[
                    ToolUseBlock(
                        id="tu1",
                        name="mcp__recon-tools__list_companies_tool",
                        input={},
                    )
                ],
                model="claude-sonnet-5",
            ),
            UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id="tu1", content=_offload_notice(missing_file)
                    )
                ]
            ),
            _result_message(),
        ],
    )

    result = agent_sdk.AgentSdkRuntime().run(CASE)

    assert result.error is None
    assert result.tool_calls[0].status == "unknown"
    assert result.tool_calls[0].elapsed_ms == 0


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
    ) -> tuple[ClaudeAgentOptions, float, agent_sdk.RunBudget]:
        raise FileNotFoundError("config/models.yaml not found")

    monkeypatch.setattr(agent_sdk, "_build_options", fake_build_options)

    result = agent_sdk.AgentSdkRuntime().run(CASE)

    assert result.error is not None
    assert "models.yaml" in result.error


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.unit
@pytest.mark.anyio
async def test_run_async_success_parses_tool_calls_and_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run_async` must produce the same result as `run` for the same
    message stream — it's the same logic, just awaited directly instead of
    bridged through `asyncio.run()`.
    """
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

    result = await agent_sdk.AgentSdkRuntime().run_async(CASE)

    assert result.error is None
    assert result.answer == "Industrials"
    assert result.tool_calls[0].tool == "list_companies"
    assert result.cost_eur == pytest.approx(0.01 * 0.9)


@pytest.mark.unit
@pytest.mark.anyio
async def test_run_async_cancellation_stops_the_underlying_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug `run_async` exists to fix: a timeout must actually stop the
    run, not just abandon the wrapper around it. Simulates `query()` hanging
    forever (as it would on a slow/stuck subprocess) and asserts that
    cancelling the awaiting coroutine (via `asyncio.wait_for`, the same
    mechanism `api/main.py` uses) reaches all the way into `query()`'s own
    cleanup — proving the cancellation signal actually propagates instead of
    being stranded in a separate event loop the way `run()` + `to_thread`
    would strand it.
    """
    _patch_options(monkeypatch)
    cleanup_ran = []

    async def hanging_query(
        *, prompt: str, options: ClaudeAgentOptions | None = None
    ) -> AsyncIterator[object]:
        try:
            await asyncio.Event().wait()
            yield object()  # pragma: no cover - unreachable, keeps this an async generator
        finally:
            cleanup_ran.append(True)

    monkeypatch.setattr(agent_sdk, "query", hanging_query)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            agent_sdk.AgentSdkRuntime().run_async(CASE), timeout=0.05
        )

    assert cleanup_ran == [True]
