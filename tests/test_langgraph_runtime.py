"""Tests for `runtimes/langgraph.py` (issue #14, part 1: single mode).

Same monkeypatching philosophy as `tests/test_runtimes.py`: fabricate the
model's responses, no real Anthropic API call, no cost. Only the chat model
is faked - tool calls go through the real local MCP server subprocess
(`tools/mcp_server.py`), the same one `AgentSdkRuntime` uses, so the tool-
result parsing is exercised against the real shape `langchain-mcp-adapters`
actually produces, not a guessed one.
"""

from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
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


@pytest.mark.unit
def test_multi_mode_not_yet_implemented() -> None:
    with pytest.raises(NotImplementedError):
        lg.LangGraphRuntime(mode="multi")
