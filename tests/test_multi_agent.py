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
import yaml
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultError,
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


_GENEROUS_RUN_BUDGET: dict[str, Any] = {
    "max_tool_calls": 1000,
    "max_tokens": 10_000_000,
    "max_wall_clock_s": 3600.0,
}


def _patch_roles_and_models(
    monkeypatch: pytest.MonkeyPatch, **model_overrides: Any
) -> None:
    monkeypatch.setattr(multi_agent, "_load_roles_config", lambda path: ROLES_CONFIG)
    model_config = {
        "usd_to_eur_rate": 0.9,
        "run_budget": _GENEROUS_RUN_BUDGET,
        **model_overrides,
    }
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


@pytest.mark.unit
def test_search_knowledge_goes_to_worker_facts_critic_and_the_investigator() -> None:
    """ADR 0025: lookups and the critic's checks search filing text; the
    supervisor routes and the lookup worker resolves ids, so neither does."""
    roles = yaml.safe_load(Path("config/roles.yaml").read_text())
    with_tool = {
        role for role, c in roles.items() if "search_knowledge" in c.get("tools", [])
    }
    assert with_tool == {"worker_facts", "critic"}
    critic = multi_agent._build_role_options(
        "critic", roles["critic"], Path("prompts"), multi_agent._CRITIC_SCHEMA
    )
    assert critic.allowed_tools == ["mcp__recon-tools__search_knowledge_tool", "Read"]
    assert "mcp__recon-tools__search_knowledge_tool" in agent_sdk.ALLOWED_TOOLS


@pytest.mark.unit
def test_sdk_tool_servers_ask_for_the_search_models_warm_up() -> None:
    """#99: an Agent SDK tool server lives for the whole case, so loading the
    models as it starts takes them off the first search's clock."""
    facts = multi_agent._build_role_options(
        "worker_facts", ROLES_CONFIG["worker_facts"], Path("prompts"), {}
    )
    single, _, _ = agent_sdk._build_options(
        Path("config/models.yaml"), Path("prompts/investigator.md")
    )
    for options in (facts, single):
        servers: Any = options.mcp_servers
        assert (
            servers[agent_sdk.MCP_SERVER_NAME]["env"]["RECON_WARM_SEARCH_MODELS"] == "1"
        )


@pytest.mark.unit
@pytest.mark.anyio
async def test_a_worker_that_fails_keeps_the_runs_cost_so_far(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#110: a worker hit its turn limit and the run recorded EUR 0.00, though
    the decompose call and the worker itself had both cost something."""
    _patch_roles_and_models(monkeypatch)

    def query(*, prompt: str, options: ClaudeAgentOptions | None = None) -> Any:
        async def gen() -> Any:
            if "Decompose this question" in prompt:
                yield _result_message(
                    total_cost_usd=0.10,
                    usage={"input_tokens": 1000, "output_tokens": 100},
                    structured_output={
                        "subtasks": [
                            {"worker": "worker_lookup", "instruction": "find it"}
                        ]
                    },
                )
                return
            yield _result_message(
                is_error=True,
                subtype="error_max_turns",
                total_cost_usd=0.05,
                usage={"input_tokens": 500, "output_tokens": 50},
                structured_output=None,
            )
            raise ResultError(
                "Claude Code returned an error result: Reached maximum number of turns (8)",
                data={"subtype": "error_max_turns"},
                exit_code=1,
            )

        return gen()

    monkeypatch.setattr(agent_sdk, "query", query)

    result = await agent_sdk.AgentSdkRuntime(mode="multi").run_async(CASE)

    assert result.error is not None and "maximum number of turns" in result.error
    assert result.cost_eur == pytest.approx((0.10 + 0.05) * 0.9)
    assert (result.tokens_in, result.tokens_out) == (1500, 150)


# --- routing (step 14, #19) ----------------------------------------------------


class _LocalModel:
    """Stands in for Ollama: records the decompose call and routes to one worker."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def complete(
        self, system_prompt: str, prompt: str, schema: dict[str, Any]
    ) -> Any:
        from recon.runtimes.providers import Reply

        self.calls.append((system_prompt, prompt))
        return Reply(
            structured={
                "subtasks": [
                    {"worker": "worker_lookup", "instruction": "find FIRM-001's sector"}
                ]
            },
            tokens_in=200,
            tokens_out=20,
            cost_eur=0.0,
        )


@pytest.mark.unit
@pytest.mark.anyio
async def test_with_routing_the_local_model_decomposes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_roles_and_models(monkeypatch)
    local = _LocalModel()
    monkeypatch.setattr(multi_agent, "local_provider", lambda config: local)
    monkeypatch.setattr(multi_agent, "routes", lambda config, step: True)
    claude_prompts: list[str] = []

    def query(*, prompt: str, options: ClaudeAgentOptions | None = None) -> Any:
        claude_prompts.append(prompt)
        return _accepting_critic_query(prompt=prompt, options=options)

    monkeypatch.setattr(agent_sdk, "query", query)

    result = await multi_agent.run_multi_async(
        CASE,
        roles_config_path=Path("unused"),
        prompts_dir=Path("prompts"),
        routing=True,
    )

    assert result.answer == "Industrials"
    assert len(local.calls) == 1
    system_prompt, prompt = local.calls[0]
    assert system_prompt == Path("prompts/supervisor.md").read_text(encoding="utf-8")
    assert CASE.question in prompt
    # Workers, synthesis and critic stay on Claude; decompose doesn't.
    assert not any("Decompose this question" in p for p in claude_prompts)
    assert len(claude_prompts) == 3


@pytest.mark.unit
@pytest.mark.anyio
async def test_without_routing_claude_decomposes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_roles_and_models(monkeypatch)
    monkeypatch.setattr(
        multi_agent, "local_provider", lambda config: pytest.fail("routed anyway")
    )
    claude_prompts: list[str] = []

    def query(*, prompt: str, options: ClaudeAgentOptions | None = None) -> Any:
        claude_prompts.append(prompt)
        return _accepting_critic_query(prompt=prompt, options=options)

    monkeypatch.setattr(agent_sdk, "query", query)

    await multi_agent.run_multi_async(
        CASE, roles_config_path=Path("unused"), prompts_dir=Path("prompts")
    )

    assert any("Decompose this question" in p for p in claude_prompts)


@pytest.mark.unit
@pytest.mark.anyio
async def test_the_sdk_runtime_passes_routing_to_multi_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def run_multi_async(case: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise RuntimeError("stop after reading the arguments")

    monkeypatch.setattr(multi_agent, "run_multi_async", run_multi_async)

    await agent_sdk.AgentSdkRuntime(mode="multi", routing=True).run_async(CASE)

    assert seen["routing"] is True


@pytest.mark.unit
@pytest.mark.anyio
async def test_local_tokens_dont_count_as_claude_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The local model is free and has no share in Claude's token budget."""
    _patch_roles_and_models(monkeypatch)
    monkeypatch.setattr(multi_agent, "local_provider", lambda config: _LocalModel())
    monkeypatch.setattr(multi_agent, "routes", lambda config, step: True)
    monkeypatch.setattr(agent_sdk, "query", _accepting_critic_query)

    routed = await multi_agent.run_multi_async(
        CASE,
        roles_config_path=Path("unused"),
        prompts_dir=Path("prompts"),
        routing=True,
    )

    # Worker, synthesis and critic: 10 in and 5 out each, from _result_message.
    assert (routed.tokens_in, routed.tokens_out) == (30, 15)
