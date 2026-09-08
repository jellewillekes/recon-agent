"""`Runtime` backed by LangGraph, against the same step-3 MCP server the SDK
runtime uses (`tools/mcp_server.py`, unchanged) via `langchain-mcp-adapters`.

See `docs/adr/0010-langgraph-runtime.md` for why this needs a real
`ANTHROPIC_API_KEY` (a second, separately-billed cost source alongside the
Agent SDK subscription credit `runtimes/agent_sdk.py` uses exclusively), why
`mcp` is pinned below 2.0 project-wide, and the tool-restriction mechanism.

Single mode (this module, issue #14 part 1/2) is `langgraph.prebuilt.
create_react_agent` — "a ReAct graph" in LangGraph's own vocabulary. Multi
mode (issue #14 part 2/2) is a hand-built `StateGraph`.
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Literal

import yaml
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StdioConnection
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel

from recon.contracts import AgentResult, Case, ToolCall

RUNTIME_NAME = "langgraph"

MCP_SERVER_NAME = "recon-tools"
_TOOL_NAMES = (
    "list_companies_tool",
    "list_financial_concepts_tool",
    "get_financial_fact_tool",
    "search_filings_tool",
    "flag_case_for_review_tool",
)

DEFAULT_MODELS_CONFIG_PATH = Path("config/models.yaml")
DEFAULT_PROMPT_PATH = Path("prompts/investigator.md")


class AnswerResponse(BaseModel):
    """Mirrors `agent_sdk._ANSWER_SCHEMA`'s shape - passed as `create_react_agent`'s
    `response_format` to get the same validated final structure.
    """

    answer: str
    evidence: list[str]
    confidence: Literal["high", "medium", "low"]


_Confidence = Literal["high", "medium", "low"]


def _load_model_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        config: dict[str, Any] = yaml.safe_load(f)
    return config


def _mcp_connection(env: dict[str, str] | None = None) -> StdioConnection:
    """One `langchain_mcp_adapters` stdio connection entry, spawning the same
    `-m recon.tools.mcp_server` subprocess the SDK runtime uses.
    """
    return StdioConnection(
        transport="stdio",
        command=sys.executable,
        args=["-m", "recon.tools.mcp_server"],
        env=env,
    )


def _compute_cost_eur(
    model_config: dict[str, Any], model: str, tokens_in: int, tokens_out: int
) -> float:
    """`ChatAnthropic` reports token counts (`usage_metadata`) but never a
    computed dollar cost the way `claude_agent_sdk`'s `ResultMessage.
    total_cost_usd` does - `config/models.yaml`'s `pricing:` table (added for
    this runtime) is what fills that gap.
    """
    pricing = model_config["pricing"][model]
    cost_usd = (
        tokens_in / 1_000_000 * pricing["input_usd_per_mtok"]
        + tokens_out / 1_000_000 * pricing["output_usd_per_mtok"]
    )
    return cost_usd * float(model_config["usd_to_eur_rate"])


def _strip_tool_name(name: str) -> str:
    return name.removesuffix("_tool")


def _parse_tool_message_status(content: Any) -> tuple[str, int]:
    """Pull `status`/`elapsed_ms` back out of the JSON our own `ToolResult`/
    `ReviewFlagResult` produced. `ToolMessage.content` from a real `ToolNode`
    run is a list of `{"type": "text", "text": "<json>"}` blocks (confirmed
    live against this project's own MCP server) - falls back to `("unknown",
    0)` for anything else, same spirit as `agent_sdk._parse_tool_result`.
    """
    text: str | None = content if isinstance(content, str) else None
    if text is None and isinstance(content, list):
        text = next(
            (
                item.get("text")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ),
            None,
        )
    if text is None:
        return "unknown", 0
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return "unknown", 0
    status = payload.get("status")
    elapsed_ms = payload.get("elapsed_ms", 0)
    if not isinstance(status, str) or not isinstance(elapsed_ms, int):
        return "unknown", 0
    return status, elapsed_ms


def _extract_tool_calls(messages: list[BaseMessage]) -> list[ToolCall]:
    """Walk the graph's final message list pairing each `AIMessage.tool_calls`
    entry with its matching `ToolMessage` by `tool_call_id`. Only tool names
    in `_TOOL_NAMES` count - `response_format`'s own internal structured-
    output tool call (a real message in this list too) is excluded by not
    matching that allowlist, the same "only our real tools count" principle
    as `agent_sdk._run_query`'s `mcp_prefix` filter.
    """
    pending: dict[str, tuple[str, dict[str, Any]]] = {}
    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                if call["name"] in _TOOL_NAMES and call["id"] is not None:
                    pending[call["id"]] = (call["name"], call["args"])
    tool_calls: list[ToolCall] = []
    for message in messages:
        if isinstance(message, ToolMessage) and message.tool_call_id in pending:
            name, args = pending[message.tool_call_id]
            status, elapsed_ms = _parse_tool_message_status(message.content)
            tool_calls.append(
                ToolCall(
                    tool=_strip_tool_name(name),
                    arguments=args,
                    status=status,
                    elapsed_ms=elapsed_ms,
                )
            )
    return tool_calls


def _sum_usage(messages: list[BaseMessage]) -> tuple[int, int]:
    tokens_in = 0
    tokens_out = 0
    for message in messages:
        if isinstance(message, AIMessage) and message.usage_metadata:
            tokens_in += message.usage_metadata.get("input_tokens", 0)
            tokens_out += message.usage_metadata.get("output_tokens", 0)
    return tokens_in, tokens_out


def _validate_answer(response: AnswerResponse) -> tuple[str, list[str], _Confidence]:
    """No confidence check needed here, unlike `agent_sdk._validate_answer`:
    that one validates a raw dict against `_CONFIDENCE_VALUES` because the
    SDK hands back unvalidated JSON. Here `response` is already a real
    `AnswerResponse` instance - its `Literal["high","medium","low"]` field
    means a bad value never gets this far; the model's own structured-
    output parsing raises first (surfaces as `AgentResult.error` the same
    way any other failure in this method does).
    """
    return response.answer, response.evidence, response.confidence


class LangGraphRuntime:
    """`Runtime` implementation driving LangGraph. `mode="multi"` lands in
    issue #14 part 2; this module currently only implements `"single"`.
    """

    def __init__(
        self,
        *,
        mode: Literal["single", "multi"] = "single",
        models_config_path: Path = DEFAULT_MODELS_CONFIG_PATH,
        prompt_path: Path = DEFAULT_PROMPT_PATH,
    ) -> None:
        if mode == "multi":
            raise NotImplementedError(
                "LangGraphRuntime(mode='multi') lands in issue #14 part 2."
            )
        self._mode = mode
        self._models_config_path = models_config_path
        self._prompt_path = prompt_path

    def run(self, case: Case) -> AgentResult:
        """Answer `case`. Never raises - see `AgentSdkRuntime.run`'s docstring;
        the `Runtime` contract requires every failure to surface as
        `AgentResult.error` instead.
        """
        return asyncio.run(self.run_async(case))

    async def run_async(self, case: Case) -> AgentResult:
        start = time.monotonic()
        try:
            model_config = _load_model_config(self._models_config_path)
            investigator = model_config["investigator"]
            model_name = investigator["model"]

            client = MultiServerMCPClient({MCP_SERVER_NAME: _mcp_connection()})
            tools = await client.get_tools()

            # mypy's stub for ChatAnthropic's generated __init__ doesn't
            # surface `model` as a valid kwarg, though it's a genuine pydantic
            # field (confirmed: ChatAnthropic.model_fields, and constructs
            # fine at runtime) - a stub gap, not a real type error.
            model = ChatAnthropic(model=model_name)  # type: ignore[call-arg]
            prompt = self._prompt_path.read_text(encoding="utf-8")
            graph = create_react_agent(
                model,
                tools,
                prompt=prompt,
                response_format=AnswerResponse,
                checkpointer=InMemorySaver(),
            )
            result = await graph.ainvoke(
                {"messages": [("user", case.question)]},
                config={
                    "configurable": {"thread_id": case.case_id},
                    # Graph *steps*, not literal turns (each tool-call round
                    # trip is ~2 steps in this prebuilt graph) - reuses
                    # investigator.max_turns as a generous, not exact, bound,
                    # rather than forking a second turn-budget config.
                    "recursion_limit": investigator["max_turns"],
                },
            )

            structured = result["structured_response"]
            if not isinstance(structured, AnswerResponse):
                raise TypeError(
                    "langgraph run produced no structured answer "
                    f"(got {type(structured)!r})."
                )
            answer, evidence, confidence = _validate_answer(structured)
            messages = result["messages"]
            tool_calls = _extract_tool_calls(messages)
            tokens_in, tokens_out = _sum_usage(messages)
            cost_eur = _compute_cost_eur(
                model_config, model_name, tokens_in, tokens_out
            )

            return AgentResult(
                case_id=case.case_id,
                answer=answer,
                evidence=evidence,
                confidence=confidence,
                tool_calls=tool_calls,
                runtime=RUNTIME_NAME,
                mode=self._mode,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_eur=cost_eur,
                elapsed_ms=int((time.monotonic() - start) * 1000),
                error=None,
            )
        except Exception as exc:  # noqa: BLE001 — boundary: see run's docstring
            return AgentResult(
                case_id=case.case_id,
                answer="",
                evidence=[],
                confidence="low",
                tool_calls=[],
                runtime=RUNTIME_NAME,
                mode=self._mode,
                tokens_in=0,
                tokens_out=0,
                cost_eur=0.0,
                elapsed_ms=int((time.monotonic() - start) * 1000),
                error=str(exc),
            )
