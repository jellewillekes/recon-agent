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
import contextlib
import json
import os
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

# Passed to the MCP server subprocess's environment, same convention as
# agent_sdk._CREATED_BY - without it, mcp_server.py's _call_flag_case_for_review
# falls back to the wrong "agent_sdk:unknown" label for every flag made
# through this runtime.
_CREATED_BY = f"{RUNTIME_NAME}:single"

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


def _budget_breach_note(tokens_in: int, tokens_out: int, max_tokens: int) -> str | None:
    """Checked once the run has already completed. Unlike `max_tool_calls`
    (enforced live in `_run_graph`, mid-stream), token usage from a breaching
    message is only known once that message's `usage_metadata` has already
    been folded into the running total, so - like `agent_sdk`'s own
    single-mode token check - this can only report a breach after the fact,
    not stop it early. Reported, not discarded - the run's answer is real
    and already complete by the time this is checked.
    """
    total_tokens = tokens_in + tokens_out
    if total_tokens > max_tokens:
        return (
            f"token budget of {max_tokens} exceeded ({total_tokens} used) - "
            "reported after the fact, since the run had already completed"
        )
    return None


class _BudgetExceeded(Exception):
    """Mirrors `agent_sdk._BudgetExceeded`: raised the instant either
    `run_budget` ceiling breaches - `max_tool_calls` mid-stream, or
    `max_wall_clock_s` on cancellation - carrying everything gathered up to
    that point so the caller returns a partial `AgentResult` instead of
    either letting a runaway loop keep spending real `ANTHROPIC_API_KEY`
    credit until `max_turns`/`recursion_limit` eventually intervenes (round 2
    review of PR #46), or discarding that partial telemetry on a wall-clock
    breach (round 3 review of PR #46).
    """

    def __init__(
        self,
        reason: str,
        tool_calls: list[ToolCall],
        tokens_in: int,
        tokens_out: int,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.tool_calls = tool_calls
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out


async def _run_graph(
    graph: Any,
    case: Case,
    max_turns: int,
    max_wall_clock_s: float,
    max_tool_calls: int,
) -> dict[str, Any]:
    """Drive one graph run to completion, bounded by `run_budget`'s
    `max_wall_clock_s` (a hard ceiling on the whole call, via
    `asyncio.wait_for`) and `max_tool_calls` (checked after every graph step,
    via `astream`'s `stream_mode="values"`, which yields the accumulated
    state after each node runs - unlike `ainvoke`, which only returns once
    the whole run is over). Both breach paths raise `_BudgetExceeded`, the
    same live fidelity `agent_sdk._run_query`'s streaming `_BudgetTracker`
    has: a tool-call breach closes the stream immediately (`contextlib.
    aclosing`, not a bare `break` - see that module's docstring for why that
    matters); a wall-clock breach cancels `_drive` via `asyncio.wait_for`,
    but still reports the tool calls and tokens gathered up to the last
    completed step instead of discarding them (round 3 review of PR #46).
    """
    config = {
        "configurable": {"thread_id": case.case_id},
        # Graph *steps*, not literal turns (each tool-call round trip is ~2
        # steps in this prebuilt graph) - reuses investigator.max_turns as a
        # generous, not exact, bound, rather than forking a second
        # turn-budget config.
        "recursion_limit": max_turns,
    }

    # Written from inside `_drive` on every step, read from the `TimeoutError`
    # handler below. A wall-clock breach cancels `_drive`'s task, but this
    # variable lives in `_run_graph`'s own frame, not the cancelled task's -
    # it still holds whatever the last completed step wrote.
    last_state: dict[str, Any] | None = None

    async def _drive() -> dict[str, Any]:
        nonlocal last_state
        stream = graph.astream(
            {"messages": [("user", case.question)]},
            config=config,
            stream_mode="values",
        )
        async with contextlib.aclosing(stream):
            async for chunk in stream:
                last_state = chunk
                tool_calls = _extract_tool_calls(chunk["messages"])
                if len(tool_calls) >= max_tool_calls:
                    tokens_in, tokens_out = _sum_usage(chunk["messages"])
                    raise _BudgetExceeded(
                        f"tool-call budget of {max_tool_calls} exceeded",
                        tool_calls=tool_calls,
                        tokens_in=tokens_in,
                        tokens_out=tokens_out,
                    )
        if last_state is None:
            raise RuntimeError("langgraph run produced no state.")
        return last_state

    try:
        return await asyncio.wait_for(_drive(), timeout=max_wall_clock_s)
    except (TimeoutError, BaseExceptionGroup) as exc:
        # asyncio.wait_for's own timeout always raises a plain TimeoutError
        # (confirmed directly against this project's own graph) - but
        # LangGraph's pregel engine runs on anyio structured concurrency
        # internally, and under load a cancellation landing mid-step can
        # instead surface as a BaseExceptionGroup ("unhandled errors in a
        # TaskGroup") wrapping the same CancelledError/TimeoutError, observed
        # live running this project's own test suite. Only treat it as a
        # wall-clock breach if it actually is one; a genuine unrelated error
        # inside that group must still propagate as itself, not get
        # mislabeled as a timeout.
        if isinstance(exc, BaseExceptionGroup):
            cancelled, _other = exc.split((asyncio.CancelledError, TimeoutError))
            if cancelled is None:
                raise
        tool_calls = _extract_tool_calls(last_state["messages"]) if last_state else []
        tokens_in, tokens_out = (
            _sum_usage(last_state["messages"]) if last_state else (0, 0)
        )
        raise _BudgetExceeded(
            f"wall-clock budget of {max_wall_clock_s}s exceeded",
            tool_calls=tool_calls,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        ) from exc


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
            run_budget = model_config["run_budget"]
            max_tool_calls = int(run_budget["max_tool_calls"])
            max_tokens = int(run_budget["max_tokens"])
            max_wall_clock_s = float(run_budget["max_wall_clock_s"])

            # Same convention as agent_sdk._build_options: the full parent
            # environment, plus RECON_CREATED_BY, so mcp_server.py's
            # flag_case_for_review_tool both reaches DATABASE_URL and labels
            # created_by correctly - env=None left DATABASE_URL to whatever
            # langchain_mcp_adapters/mcp's stdio client defaults to, which is
            # not guaranteed to be the full parent environment.
            env = {**os.environ, "RECON_CREATED_BY": _CREATED_BY}
            # No explicit teardown here: per langchain-mcp-adapters' documented
            # design (since its 0.1 release), MultiServerMCPClient doesn't hold
            # a persistent session past get_tools() - each bound tool opens and
            # closes its own stdio subprocess per invocation, unlike
            # agent_sdk.py's one long-lived query() stream (contextlib.aclosing
            # in _run_query). Not independently re-verified against this
            # project's exact pinned version - see PR #46 review discussion.
            client = MultiServerMCPClient({MCP_SERVER_NAME: _mcp_connection(env=env)})
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
            result = await _run_graph(
                graph,
                case,
                investigator["max_turns"],
                max_wall_clock_s,
                max_tool_calls,
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
                error=_budget_breach_note(tokens_in, tokens_out, max_tokens),
            )
        except _BudgetExceeded as exc:
            return AgentResult(
                case_id=case.case_id,
                answer="",
                evidence=[],
                confidence="low",
                tool_calls=exc.tool_calls,
                runtime=RUNTIME_NAME,
                mode=self._mode,
                tokens_in=exc.tokens_in,
                tokens_out=exc.tokens_out,
                cost_eur=_compute_cost_eur(
                    model_config, model_name, exc.tokens_in, exc.tokens_out
                ),
                elapsed_ms=int((time.monotonic() - start) * 1000),
                error=exc.reason,
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
