"""Tests for `runtimes/langgraph.py` (issue #14, part 1: single mode).

Same monkeypatching philosophy as `tests/test_runtimes.py`: fabricate the
model's responses, no real Anthropic API call, no cost. Only the chat model
is faked - tool calls go through the real local MCP server subprocess
(`tools/mcp_server.py`), the same one `AgentSdkRuntime` uses, so the tool-
result parsing is exercised against the real shape `langchain-mcp-adapters`
actually produces, not a guessed one.
"""

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import BaseModel, Field

from recon.contracts import Case
from recon.runtimes import langgraph as lg

pytestmark = pytest.mark.unit


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


class _FakeStructuredOutput:
    """What `model.with_structured_output(schema)` returns - `create_react_agent`
    calls `.ainvoke(messages, config)` on it directly once the main loop ends.
    """

    def __init__(self, model: "_FakeToolCallingModel", schema: type[BaseModel]) -> None:
        self._model = model
        self._schema = schema

    async def ainvoke(
        self, messages: Any, config: Any = None, **kwargs: Any
    ) -> BaseModel:
        response = self._model.responses[self._model._idx]
        self._model._idx += 1
        args = response.tool_calls[0]["args"]
        return self._schema(**args)


class _FakeToolCallingModel(BaseChatModel):
    """A minimal `BaseChatModel` returning canned responses in order - none of
    `langchain_core`'s built-in fakes implement `bind_tools`/
    `with_structured_output` (both raise `NotImplementedError` by default),
    so this is the standard pattern for testing a LangGraph agent.
    """

    responses: list[AIMessage] = Field(default_factory=list)
    _idx: int = 0

    def bind_tools(self, tools: Any, **kwargs: Any) -> "_FakeToolCallingModel":
        return self

    def with_structured_output(  # type: ignore[override]
        self, schema: type[BaseModel], **kwargs: Any
    ) -> _FakeStructuredOutput:
        return _FakeStructuredOutput(self, schema)

    def _generate(
        self,
        messages: Any,
        stop: Any = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        message = self.responses[self._idx]
        self._idx += 1
        return ChatResult(generations=[ChatGeneration(message=message)])

    @property
    def _llm_type(self) -> str:
        return "fake-tool-calling"


def _patch_model(monkeypatch: pytest.MonkeyPatch, responses: list[AIMessage]) -> None:
    fake = _FakeToolCallingModel(responses=responses)
    monkeypatch.setattr(lg, "ChatAnthropic", lambda **kwargs: fake)


def _model_config(**run_budget_overrides: Any) -> dict[str, Any]:
    """Same shape as `config/models.yaml`, with a generous default `run_budget`
    - individual tests override just the ceiling they're exercising, the same
    way `tests/test_reliability.py` builds a custom `agent_sdk.RunBudget` per
    test instead of writing a real yaml file.
    """
    run_budget: dict[str, int | float] = {
        "max_tool_calls": 30,
        "max_tokens": 300_000,
        "max_wall_clock_s": 100.0,
    }
    run_budget.update(run_budget_overrides)
    return {
        "investigator": {"model": "claude-sonnet-5", "max_turns": 20},
        "pricing": {
            "claude-sonnet-5": {
                "input_usd_per_mtok": 2.00,
                "output_usd_per_mtok": 10.00,
            }
        },
        "usd_to_eur_rate": 0.92,
        "run_budget": run_budget,
    }


@pytest.mark.anyio
async def test_run_success_parses_tool_calls_and_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_model(
        monkeypatch,
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "list_companies_tool",
                        "args": {"sector": "Industrials"},
                        "id": "call1",
                    }
                ],
                usage_metadata={
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                },
            ),
            AIMessage(
                content="Industrials.",
                usage_metadata={
                    "input_tokens": 50,
                    "output_tokens": 10,
                    "total_tokens": 60,
                },
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "AnswerResponse",
                        "args": {
                            "answer": "Industrials",
                            "evidence": ["FIRM-001 is in Industrials"],
                            "confidence": "high",
                        },
                        "id": "r1",
                    }
                ],
            ),
        ],
    )

    result = await lg.LangGraphRuntime().run_async(CASE)

    assert result.error is None
    assert result.answer == "Industrials"
    assert result.evidence == ["FIRM-001 is in Industrials"]
    assert result.confidence == "high"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].tool == "list_companies"
    assert result.tool_calls[0].status == "ok"
    assert result.tool_calls[0].arguments == {"sector": "Industrials"}
    assert result.tokens_in == 150
    assert result.tokens_out == 30
    # config/models.yaml pricing for claude-sonnet-5: $2/$10 per Mtok, times
    # usd_to_eur_rate=0.92.
    expected_cost_usd = 150 / 1_000_000 * 2.00 + 30 / 1_000_000 * 10.00
    assert result.cost_eur == pytest.approx(expected_cost_usd * 0.92)
    assert result.runtime == "langgraph"
    assert result.mode == "single"


@pytest.mark.anyio
async def test_run_excludes_structured_response_tool_from_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `AnswerResponse` structured-output call is itself a tool-call-
    shaped message in the graph's final state - it must not be mistaken for
    a real MCP tool call.
    """
    _patch_model(
        monkeypatch,
        [
            AIMessage(content="No tools needed."),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "AnswerResponse",
                        "args": {
                            "answer": "unknown",
                            "evidence": [],
                            "confidence": "low",
                        },
                        "id": "r1",
                    }
                ],
            ),
        ],
    )

    result = await lg.LangGraphRuntime().run_async(CASE)

    assert result.error is None
    assert result.tool_calls == []


@pytest.mark.anyio
async def test_run_malformed_structured_output_populates_error_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`AnswerResponse`'s own `Literal["high","medium","low"]` field rejects
    a bad confidence value before `LangGraphRuntime` ever sees it - this
    proves that failure (raised deep inside `graph.ainvoke`, during
    structured-output parsing) still surfaces as a graceful `AgentResult`,
    not a crash.
    """
    _patch_model(
        monkeypatch,
        [
            AIMessage(content="ok"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "AnswerResponse",
                        "args": {
                            "answer": "x",
                            "evidence": [],
                            "confidence": "not-a-real-value",
                        },
                        "id": "r1",
                    }
                ],
            ),
        ],
    )

    result = await lg.LangGraphRuntime().run_async(CASE)

    assert result.error is not None
    assert "confidence" in result.error
    assert result.answer == ""
    assert result.tool_calls == []


@pytest.mark.anyio
async def test_run_missing_model_config_populates_error_not_raise() -> None:
    result = await lg.LangGraphRuntime(
        models_config_path=Path("does/not/exist.yaml")
    ).run_async(CASE)

    assert result.error is not None
    assert result.answer == ""
    assert result.tool_calls == []


# --- run_budget / RECON_CREATED_BY (round 1 review of PR #46) --------------


@pytest.mark.anyio
async def test_run_passes_created_by_and_parent_env_to_mcp_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`mcp_server.py`'s `_call_flag_case_for_review` reads `RECON_CREATED_BY`
    (falls back to the wrong "agent_sdk:unknown" label if unset) and
    `DATABASE_URL` straight from `os.environ` - both must reach the
    subprocess, or every `flag_case_for_review` call made through this
    runtime either mislabels itself or can't reach Postgres at all.
    `_mcp_connection()` used to be called with no `env` at all.
    """
    captured: dict[str, Any] = {}

    class _FakeClient:
        def __init__(self, connections: dict[str, Any]) -> None:
            captured["connections"] = connections

        async def get_tools(self) -> list[Any]:
            raise RuntimeError("stop before spawning a real subprocess")

    monkeypatch.setattr(lg, "MultiServerMCPClient", _FakeClient)
    monkeypatch.setenv("RECON_TEST_PARENT_VAR", "present")

    result = await lg.LangGraphRuntime().run_async(CASE)

    assert result.error == "stop before spawning a real subprocess"
    connection = captured["connections"][lg.MCP_SERVER_NAME]
    assert connection["env"]["RECON_CREATED_BY"] == "langgraph:single"
    assert connection["env"]["RECON_TEST_PARENT_VAR"] == "present"
    assert connection["env"]["PATH"] == os.environ["PATH"]


@pytest.mark.anyio
async def test_run_stops_on_wall_clock_budget_breach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 1 review of PR #46: `run_budget` was entirely bypassed here, with
    only an approximate `recursion_limit` as a cost guard on a runtime that
    spends real, separately-billed money. `max_wall_clock_s` is now a live,
    hard ceiling on the whole `graph.ainvoke` call.
    """
    monkeypatch.setattr(
        lg, "_load_model_config", lambda path: _model_config(max_wall_clock_s=0.02)
    )
    fake = _FakeToolCallingModel(responses=[AIMessage(content="ok")])

    async def slow_agenerate(
        self: _FakeToolCallingModel,
        messages: Any,
        stop: Any = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        await asyncio.sleep(1.0)
        message = self.responses[self._idx]
        self._idx += 1
        return ChatResult(generations=[ChatGeneration(message=message)])

    monkeypatch.setattr(_FakeToolCallingModel, "_agenerate", slow_agenerate)
    monkeypatch.setattr(lg, "ChatAnthropic", lambda **kwargs: fake)

    result = await lg.LangGraphRuntime().run_async(CASE)

    assert result.error is not None
    assert "wall-clock budget" in result.error
    assert result.answer == ""


@pytest.mark.anyio
async def test_run_wall_clock_breach_preserves_partial_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 3 review of PR #46: a wall-clock breach used to convert into a
    bare `RuntimeError` inside the generic `except Exception` handler,
    discarding every tool call and token gathered before the timeout. It now
    raises `_BudgetExceeded` like the tool-call-budget path does, carrying
    forward whatever the last completed graph step (here, the real tool call
    below) had already recorded.
    """
    # Real subprocess + a real tool round trip happen before the intentional
    # hang below - 1.0s was too tight on a loaded CI runner and made this
    # flaky (the budget could fire before that first, real call finished,
    # leaving no telemetry to preserve and failing the very assertion this
    # test exists to make). 3.0s gives that call generous headroom while
    # still landing well before the 10s hang.
    monkeypatch.setattr(
        lg, "_load_model_config", lambda path: _model_config(max_wall_clock_s=3.0)
    )
    fake = _FakeToolCallingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "list_companies_tool",
                        "args": {"sector": "Industrials"},
                        "id": "call1",
                    }
                ],
                usage_metadata={
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                },
            ),
            AIMessage(content="Industrials."),
        ]
    )

    async def _agenerate_hangs_after_first_call(
        self: _FakeToolCallingModel,
        messages: Any,
        stop: Any = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        if self._idx > 0:
            await asyncio.sleep(10.0)
        message = self.responses[self._idx]
        self._idx += 1
        return ChatResult(generations=[ChatGeneration(message=message)])

    monkeypatch.setattr(
        _FakeToolCallingModel, "_agenerate", _agenerate_hangs_after_first_call
    )
    monkeypatch.setattr(lg, "ChatAnthropic", lambda **kwargs: fake)

    result = await lg.LangGraphRuntime().run_async(CASE)

    assert result.error is not None
    assert "wall-clock budget" in result.error
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].tool == "list_companies"
    assert result.tool_calls[0].status == "ok"
    assert result.tokens_in == 100
    assert result.tokens_out == 20


@pytest.mark.anyio
async def test_run_reports_token_budget_breach_after_the_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single mode makes exactly one `graph.ainvoke` call - like `agent_sdk`'s
    own single mode, a token breach can only be reported once the (already
    complete, valid) answer exists, not prevented before the fact.
    """
    monkeypatch.setattr(
        lg, "_load_model_config", lambda path: _model_config(max_tokens=1)
    )
    _patch_model(
        monkeypatch,
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "list_companies_tool",
                        "args": {"sector": "Industrials"},
                        "id": "call1",
                    }
                ],
                usage_metadata={
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                },
            ),
            AIMessage(
                content="Industrials.",
                usage_metadata={
                    "input_tokens": 50,
                    "output_tokens": 10,
                    "total_tokens": 60,
                },
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "AnswerResponse",
                        "args": {
                            "answer": "Industrials",
                            "evidence": ["FIRM-001 is in Industrials"],
                            "confidence": "high",
                        },
                        "id": "r1",
                    }
                ],
            ),
        ],
    )

    result = await lg.LangGraphRuntime().run_async(CASE)

    assert result.error is not None
    assert "token budget" in result.error
    # Reported, not discarded - the call had already produced a real answer.
    assert result.answer == "Industrials"
    assert result.confidence == "high"


@pytest.mark.anyio
async def test_run_stops_early_on_tool_call_budget_breach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 2 review of PR #46: `max_tool_calls` used to only be checked once
    `graph.ainvoke` had already returned - unlike `agent_sdk.py`'s mid-stream
    `_BudgetTracker`, a runaway tool-calling loop here kept running (and
    spending real `ANTHROPIC_API_KEY` credit) until `max_turns`/
    `recursion_limit` intervened. Now checked after every graph step
    (`astream`'s `stream_mode="values"`), so the breach stops the run
    immediately - the model's next call (which would have produced a final
    answer) never fires.
    """
    # max_tool_calls=1, not 0: astream's stream_mode="values" yields a chunk
    # for the initial input too (0 tool calls used), and a 0-vs.->=0 ceiling
    # would trip on that chunk before the model ever runs. 1 isolates the
    # breach to right after the one real tool call completes.
    monkeypatch.setattr(
        lg, "_load_model_config", lambda path: _model_config(max_tool_calls=1)
    )
    _patch_model(
        monkeypatch,
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "list_companies_tool",
                        "args": {"sector": "Industrials"},
                        "id": "call1",
                    }
                ],
            ),
        ],
    )

    result = await lg.LangGraphRuntime().run_async(CASE)

    assert result.error is not None
    assert "tool-call budget" in result.error
    assert result.answer == ""
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].tool == "list_companies"


# --- _run_graph's BaseExceptionGroup handling (round 3 review of PR #46,
# found while re-verifying the fix: under load, asyncio.wait_for's
# cancellation can surface through LangGraph's anyio-based internals as a
# BaseExceptionGroup instead of a plain TimeoutError) ------------------------


class _FakeGraphRaisingExceptionGroup:
    """A minimal stand-in for `graph`, whose `.astream()` yields one real
    chunk then raises a `BaseExceptionGroup` - reproduces the shape observed
    live, deterministically, instead of depending on real timing.
    """

    def __init__(self, group: BaseExceptionGroup[Any]) -> None:
        self._group = group

    def astream(self, input: Any, config: Any = None, stream_mode: Any = None) -> Any:
        async def gen() -> Any:
            # A full round trip (AIMessage's tool_calls paired with its
            # ToolMessage) - not just the request half - since
            # _extract_tool_calls only records a call once it sees the
            # matching result, the same as agent_sdk._run_query does.
            yield {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "list_companies_tool",
                                "args": {"sector": "Industrials"},
                                "id": "call1",
                            }
                        ],
                        usage_metadata={
                            "input_tokens": 10,
                            "output_tokens": 5,
                            "total_tokens": 15,
                        },
                    ),
                    ToolMessage(
                        tool_call_id="call1",
                        content='{"status": "ok", "elapsed_ms": 2}',
                    ),
                ]
            }
            raise self._group

        return gen()


@pytest.mark.anyio
async def test_run_graph_treats_a_cancellation_shaped_exception_group_as_wall_clock_breach() -> (
    None
):
    group: BaseExceptionGroup[Any] = BaseExceptionGroup(
        "unhandled errors in a TaskGroup", [asyncio.CancelledError()]
    )
    graph = _FakeGraphRaisingExceptionGroup(group)

    with pytest.raises(lg._BudgetExceeded) as exc_info:
        await lg._run_graph(graph, CASE, 20, 100.0, 30)

    assert "wall-clock budget" in exc_info.value.reason
    assert len(exc_info.value.tool_calls) == 1
    assert exc_info.value.tool_calls[0].tool == "list_companies"
    assert exc_info.value.tokens_in == 10
    assert exc_info.value.tokens_out == 5


@pytest.mark.anyio
async def test_run_graph_reraises_an_unrelated_exception_group_unchanged() -> None:
    """A `BaseExceptionGroup` that does *not* contain a cancellation/timeout
    is a genuine unrelated failure - it must propagate as itself, not get
    mislabeled as a wall-clock breach.
    """
    group: BaseExceptionGroup[Any] = BaseExceptionGroup(
        "unhandled errors in a TaskGroup", [ValueError("a real, unrelated bug")]
    )
    graph = _FakeGraphRaisingExceptionGroup(group)

    with pytest.raises(BaseExceptionGroup) as exc_info:
        await lg._run_graph(graph, CASE, 20, 100.0, 30)

    assert exc_info.value is group


@pytest.mark.anyio
async def test_run_graph_surfaces_an_unrelated_exception_alongside_a_real_cancellation() -> (
    None
):
    """Round 4 review of PR #46: a mixed group (a real cancellation *and* an
    unrelated exception together) used to still report a plain wall-clock
    breach, silently discarding the unrelated exception rather than just
    not mislabeling it as the cause. Graceful degradation for the
    cancellation still applies, but the other failure must be visible too.
    """
    other = ValueError("a real, unrelated bug that happened at the same time")
    group: BaseExceptionGroup[Any] = BaseExceptionGroup(
        "unhandled errors in a TaskGroup", [asyncio.CancelledError(), other]
    )
    graph = _FakeGraphRaisingExceptionGroup(group)

    with pytest.raises(lg._BudgetExceeded) as exc_info:
        await lg._run_graph(graph, CASE, 20, 100.0, 30)

    assert "wall-clock budget" in exc_info.value.reason
    assert repr(other) in exc_info.value.reason
    # Still degrades gracefully - the cancellation's own telemetry survives.
    assert len(exc_info.value.tool_calls) == 1


# --- _compute_cost_eur (round 4 review of PR #46) ---------------------------


def test_compute_cost_eur_returns_zero_for_a_model_with_no_pricing_entry() -> None:
    """A `KeyError` here used to be unrecoverable from inside `run_async`'s
    `except _BudgetExceeded` handler - a second `except` block can't catch a
    new exception raised while handling the first, so this would have broken
    the "never raises" contract on nothing worse than a config gap.
    """
    config = _model_config()
    del config["pricing"]["claude-sonnet-5"]

    cost = lg._compute_cost_eur(config, "claude-sonnet-5", 100, 20)

    assert cost == 0.0


@pytest.mark.anyio
async def test_run_reports_wall_clock_breach_even_with_no_pricing_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        lg,
        "_load_model_config",
        lambda path: {
            **_model_config(max_wall_clock_s=0.01),
            "pricing": {},
        },
    )

    async def _agenerate_hangs(
        self: _FakeToolCallingModel,
        messages: Any,
        stop: Any = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        await asyncio.sleep(10.0)
        raise AssertionError("unreachable")

    fake = _FakeToolCallingModel(responses=[AIMessage(content="ok")])
    monkeypatch.setattr(_FakeToolCallingModel, "_agenerate", _agenerate_hangs)
    monkeypatch.setattr(lg, "ChatAnthropic", lambda **kwargs: fake)

    result = await lg.LangGraphRuntime().run_async(CASE)

    assert result.error is not None
    assert "wall-clock budget" in result.error
    assert result.cost_eur == 0.0
