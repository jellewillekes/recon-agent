"""Tests for `runtimes/multi_agent.py`.

Same monkeypatching philosophy as `tests/test_runtimes.py`: fabricate
`claude_agent_sdk.query`'s message stream, no real model call. `query` is
patched once per test and branches on the prompt text to return the right
canned response for whichever role/call is asking (supervisor's decompose
vs. synthesize calls share a prompt *file* but not prompt *text*).
"""

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
from recon.runtimes import agent_sdk, multi_agent


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


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

ROLES_CONFIG: dict[str, Any] = {
    "supervisor": {
        "model": "claude-sonnet-5",
        "max_turns": 4,
        "tools": ["flag_case_for_review"],
    },
    "worker_lookup": {
        "model": "claude-sonnet-5",
        "max_turns": 8,
        "tools": ["list_companies", "list_financial_concepts"],
    },
    "worker_facts": {
        "model": "claude-sonnet-5",
        "max_turns": 8,
        "tools": ["get_financial_fact", "search_filings"],
    },
    "critic": {"model": "claude-sonnet-5", "max_turns": 3},
}


def _result_message(**overrides: Any) -> ResultMessage:
    defaults: dict[str, Any] = {
        "subtype": "success",
        "duration_ms": 100,
        "duration_api_ms": 80,
        "is_error": False,
        "num_turns": 1,
        "session_id": "session-1",
        "total_cost_usd": 0.001,
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "structured_output": {},
    }
    defaults.update(overrides)
    return ResultMessage(**defaults)


def _patch_roles_and_models(
    monkeypatch: pytest.MonkeyPatch, **model_overrides: Any
) -> None:
    monkeypatch.setattr(multi_agent, "_load_roles_config", lambda path: ROLES_CONFIG)
    model_config = {"usd_to_eur_rate": 0.9, **model_overrides}
    monkeypatch.setattr(multi_agent, "_load_model_config", lambda path: model_config)


def _accepting_critic_query(
    *, prompt: str, options: ClaudeAgentOptions | None = None
) -> Any:
    async def gen() -> Any:
        if "Decompose this question" in prompt:
            yield _result_message(
                structured_output={
                    "subtasks": [
                        {
                            "worker": "worker_lookup",
                            "instruction": "find FIRM-001's sector",
                        }
                    ]
                }
            )
        elif "Synthesize a final answer" in prompt:
            yield _result_message(
                structured_output={
                    "answer": "Industrials",
                    "evidence": ["FIRM-001 is in Industrials"],
                    "confidence": "high",
                }
            )
        elif "Does the evidence support" in prompt:
            yield _result_message(
                structured_output={"accepted": True, "reason": "well supported"}
            )
        else:
            # A worker call.
            yield AssistantMessage(
                content=[
                    ToolUseBlock(
                        id="tu1",
                        name="mcp__recon-tools__list_companies_tool",
                        input={"sector": None},
                    )
                ],
                model="claude-sonnet-5",
            )
            yield UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id="tu1",
                        content='{"status": "ok", "elapsed_ms": 5}',
                    )
                ]
            )
            yield _result_message(
                structured_output={
                    "findings": "FIRM-001 is in Industrials",
                    "evidence": ["FIRM-001 sector=Industrials"],
                }
            )

    return gen()


@pytest.mark.unit
@pytest.mark.anyio
async def test_run_multi_success_full_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_roles_and_models(monkeypatch)
    monkeypatch.setattr(agent_sdk, "query", _accepting_critic_query)

    result = await multi_agent.run_multi_async(
        CASE, roles_config_path=Path("unused"), prompts_dir=Path("prompts")
    )

    assert result.answer == "Industrials"
    assert result.confidence == "high"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].tool == "list_companies"
    # 4 calls: decompose, 1 worker, synthesize, critic.
    assert result.cost_eur == pytest.approx(0.001 * 0.9 * 4)


def _flagging_supervisor_query(
    *, prompt: str, options: ClaudeAgentOptions | None = None
) -> Any:
    """Same shape as `_accepting_critic_query`, but the supervisor's
    decompose call also calls `flag_case_for_review_tool` before returning
    its structured output - the supervisor is the only role that can reach
    this tool, so this is the only call site that can prove its result makes
    it into `AgentResult.tool_calls`.
    """

    async def gen() -> Any:
        if "Decompose this question" in prompt:
            yield AssistantMessage(
                content=[
                    ToolUseBlock(
                        id="tu-flag",
                        name="mcp__recon-tools__flag_case_for_review_tool",
                        input={"case_id": CASE.case_id, "dry_run": True},
                    )
                ],
                model="claude-sonnet-5",
            )
            yield UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id="tu-flag",
                        content='{"status": "would_write", "message": "preview"}',
                    )
                ]
            )
            yield _result_message(
                structured_output={
                    "subtasks": [
                        {
                            "worker": "worker_lookup",
                            "instruction": "find FIRM-001's sector",
                        }
                    ]
                }
            )
        elif "Synthesize a final answer" in prompt:
            yield _result_message(
                structured_output={
                    "answer": "Industrials",
                    "evidence": ["FIRM-001 is in Industrials"],
                    "confidence": "high",
                }
            )
        elif "Does the evidence support" in prompt:
            yield _result_message(
                structured_output={"accepted": True, "reason": "well supported"}
            )
        else:
            yield _result_message(
                structured_output={
                    "findings": "FIRM-001 is in Industrials",
                    "evidence": ["FIRM-001 sector=Industrials"],
                }
            )

    return gen()


@pytest.mark.unit
@pytest.mark.anyio
async def test_run_multi_includes_supervisor_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_accumulate` folds in every `_QueryResult.tool_calls`, not just the
    worker loop's - a review-flag write only the supervisor can make must
    not vanish from `AgentResult.tool_calls`.
    """
    _patch_roles_and_models(monkeypatch)
    monkeypatch.setattr(agent_sdk, "query", _flagging_supervisor_query)

    result = await multi_agent.run_multi_async(
        CASE, roles_config_path=Path("unused"), prompts_dir=Path("prompts")
    )

    assert [call.tool for call in result.tool_calls] == ["flag_case_for_review"]
    assert result.tool_calls[0].status == "would_write"


@pytest.mark.unit
@pytest.mark.anyio
async def test_run_multi_routes_to_both_workers_in_one_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The success-path test above only routes to one worker - this exercises
    the loop body's second iteration, proving two different roles' restricted
    options each get built and called, in the decomposition's order.
    """
    _patch_roles_and_models(monkeypatch)
    worker_prompts: list[str] = []

    def two_worker_query(
        *, prompt: str, options: ClaudeAgentOptions | None = None
    ) -> Any:
        async def gen() -> Any:
            if "Decompose this question" in prompt:
                yield _result_message(
                    structured_output={
                        "subtasks": [
                            {"worker": "worker_lookup", "instruction": "find FIRM-001"},
                            {
                                "worker": "worker_facts",
                                "instruction": "get its revenue",
                            },
                        ]
                    }
                )
            elif "Synthesize a final answer" in prompt:
                yield _result_message(
                    structured_output={
                        "answer": "Industrials, revenue reported",
                        "evidence": ["FIRM-001 sector", "FIRM-001 revenue"],
                        "confidence": "high",
                    }
                )
            elif "Does the evidence support" in prompt:
                yield _result_message(
                    structured_output={"accepted": True, "reason": "supported"}
                )
            else:
                worker_prompts.append(prompt)
                yield _result_message(
                    structured_output={"findings": f"found: {prompt}", "evidence": []}
                )

        return gen()

    monkeypatch.setattr(agent_sdk, "query", two_worker_query)

    result = await multi_agent.run_multi_async(
        CASE, roles_config_path=Path("unused"), prompts_dir=Path("prompts")
    )

    assert worker_prompts == ["find FIRM-001", "get its revenue"]
    assert result.answer == "Industrials, revenue reported"


@pytest.mark.unit
@pytest.mark.anyio
async def test_critic_rejection_forces_confidence_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_roles_and_models(monkeypatch)

    def rejecting_query(
        *, prompt: str, options: ClaudeAgentOptions | None = None
    ) -> Any:
        async def gen() -> Any:
            if "Decompose this question" in prompt:
                yield _result_message(
                    structured_output={
                        "subtasks": [
                            {"worker": "worker_lookup", "instruction": "look it up"}
                        ]
                    }
                )
            elif "Synthesize a final answer" in prompt:
                yield _result_message(
                    structured_output={
                        "answer": "Industrials",
                        "evidence": ["a guess"],
                        "confidence": "high",
                    }
                )
            elif "Does the evidence support" in prompt:
                yield _result_message(
                    structured_output={
                        "accepted": False,
                        "reason": "evidence doesn't back the claim",
                    }
                )
            else:
                yield _result_message(
                    structured_output={"findings": "nothing found", "evidence": []}
                )

        return gen()

    monkeypatch.setattr(agent_sdk, "query", rejecting_query)

    result = await multi_agent.run_multi_async(
        CASE, roles_config_path=Path("unused"), prompts_dir=Path("prompts")
    )

    assert result.confidence == "low"


@pytest.mark.unit
def test_worker_options_exclude_tools_outside_subset() -> None:
    lookup_options = multi_agent._build_role_options(
        "worker_lookup",
        ROLES_CONFIG["worker_lookup"],
        Path("prompts"),
        multi_agent._WORKER_SCHEMA,
    )
    facts_options = multi_agent._build_role_options(
        "worker_facts",
        ROLES_CONFIG["worker_facts"],
        Path("prompts"),
        multi_agent._WORKER_SCHEMA,
    )

    assert lookup_options.allowed_tools is not None
    assert (
        "mcp__recon-tools__get_financial_fact_tool" not in lookup_options.allowed_tools
    )
    assert "mcp__recon-tools__search_filings_tool" not in lookup_options.allowed_tools
    assert "mcp__recon-tools__list_companies_tool" in lookup_options.allowed_tools
    assert (
        "mcp__recon-tools__flag_case_for_review_tool"
        not in lookup_options.allowed_tools
    )

    assert facts_options.allowed_tools is not None
    assert "mcp__recon-tools__list_companies_tool" not in facts_options.allowed_tools
    assert (
        "mcp__recon-tools__flag_case_for_review_tool" not in facts_options.allowed_tools
    )
    assert (
        "mcp__recon-tools__list_financial_concepts_tool"
        not in facts_options.allowed_tools
    )
    assert "mcp__recon-tools__get_financial_fact_tool" in facts_options.allowed_tools


@pytest.mark.unit
def test_critic_gets_no_mcp_server_at_all() -> None:
    """Not just an empty allowlist - no MCP server attached, so no tool is
    structurally absent from the critic's client, not merely refused.
    """
    critic_options = multi_agent._build_role_options(
        "critic", ROLES_CONFIG["critic"], Path("prompts"), multi_agent._CRITIC_SCHEMA
    )

    assert critic_options.mcp_servers == {}
    assert critic_options.allowed_tools == []


@pytest.mark.unit
def test_supervisor_gets_only_the_flag_tool() -> None:
    """The supervisor's one tool is flag_case_for_review (docs/contracts.md
    section 6: "supervisor only") - none of the workers' read tools.
    """
    supervisor_options = multi_agent._build_role_options(
        "supervisor",
        ROLES_CONFIG["supervisor"],
        Path("prompts"),
        multi_agent._DECOMPOSE_SCHEMA,
    )

    assert supervisor_options.allowed_tools == [
        "mcp__recon-tools__flag_case_for_review_tool",
        "Read",
    ]
    for read_tool in (
        "list_companies_tool",
        "list_financial_concepts_tool",
        "get_financial_fact_tool",
        "search_filings_tool",
    ):
        assert f"mcp__recon-tools__{read_tool}" not in supervisor_options.allowed_tools
