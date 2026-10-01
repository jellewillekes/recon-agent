"""`Runtime` backed by LangGraph, against the same step-3 MCP server the SDK
runtime uses (`tools/mcp_server.py`, unchanged) via `langchain-mcp-adapters`.

See `docs/adr/0010-langgraph-runtime.md` for why this needs a real
`ANTHROPIC_API_KEY` (a second, separately-billed cost source alongside the
Agent SDK subscription credit `runtimes/agent_sdk.py` uses exclusively), why
`mcp` is pinned below 2.0 project-wide, and the tool-restriction mechanism.

Single mode (this module) is `langgraph.prebuilt.create_react_agent` — "a
ReAct graph" in LangGraph's own vocabulary. Multi mode is a hand-built
`StateGraph` in `runtimes/langgraph_multi.py` — same split as `agent_sdk.py`/
`multi_agent.py` (ADR-0007), and for the same reason: this module is already
long with single mode's own streaming/budget mechanics, which multi mode
reuses (`_run_graph`, `_build_react_subgraph`, `_build_checkpointer`) rather
than duplicating.
"""

import asyncio
import contextlib
import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StdioConnection
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.prebuilt import create_react_agent
from psycopg import AsyncConnection
from psycopg.rows import dict_row
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

    `flag_reason` is multi mode's own extension (issue #14 part 2): the
    supervisor's synthesize call uses this same schema (`langgraph_multi.py`),
    and sets this field instead of calling a bound tool - see
    `docs/adr/0010-langgraph-runtime.md` for why. Always `None` in single
    mode, which has no dedicated confirm-flag node to act on it.
    """

    answer: str
    evidence: list[str]
    confidence: Literal["high", "medium", "low"]
    flag_reason: str | None = None


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


async def _build_checkpointer(database_url: str | None) -> BaseCheckpointSaver:
    """`AsyncPostgresSaver` when `DATABASE_URL` is configured, `InMemorySaver`
    otherwise - same "not configured is a normal, expected state" stance
    `api/health.py:check_postgres` already takes (no live Postgres exists
    anywhere in this project yet; `docker/compose.yaml` is step 10).

    Connects the same way `AsyncPostgresSaver.from_conn_string` does
    internally (autocommit, `prepare_threshold=0`, dict rows) but without its
    `async with` scoping - the connection has to outlive one call, since
    `LangGraphRuntime` caches this checkpointer on the instance and reuses it
    across a paused `run_async` and the `resume` that later completes it
    (`LangGraphRuntime._get_checkpointer`); closing it in between would lose
    exactly the state a resume needs. Within one runtime instance, that
    within-process interrupt/resume cycle works identically whichever
    checkpointer this returns - only a resume from a *different* process
    needs the real, Postgres-backed one.
    """
    if not database_url:
        return InMemorySaver()
    conn = await AsyncConnection.connect(
        database_url, autocommit=True, prepare_threshold=0, row_factory=dict_row
    )
    checkpointer = AsyncPostgresSaver(conn=conn)
    await checkpointer.setup()
    return checkpointer


def _compute_cost_eur(
    model_config: dict[str, Any], model: str, tokens_in: int, tokens_out: int
) -> float:
    """`ChatAnthropic` reports token counts (`usage_metadata`) but never a
    computed dollar cost the way `claude_agent_sdk`'s `ResultMessage.
    total_cost_usd` does - `config/models.yaml`'s `pricing:` table (added for
    this runtime) is what fills that gap.

    Returns `0.0`, never raises, if `model` has no `pricing:` entry - round 4
    of PR #46's review found this unguarded: a `KeyError` here would have
    been unrecoverable from inside `run_async`'s own `except _BudgetExceeded`
    handler (a second `except` block can't catch a new exception raised
    while handling the first), breaking the "never raises" contract on
    nothing worse than a config gap. Reported cost being wrong is far
    better than the whole run crashing over it.
    """
    pricing = model_config.get("pricing", {}).get(model)
    if pricing is None:
        return 0.0
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
        cost_eur: float = 0.0,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.tool_calls = tool_calls
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        # Single mode leaves this at 0.0 and lets run_async compute it from
        # its one well-defined model_name instead (unchanged from part 1) -
        # multi mode (langgraph_multi.py) has no single model_name to hand
        # run_async, so it computes cost itself, at the point it re-raises a
        # _BudgetExceeded surfaced from _run_graph, and carries it here.
        self.cost_eur = cost_eur


@dataclass
class _Outcome:
    """What a completed (non-erroring, non-paused) run produced, before
    `elapsed_ms` is known - shared by both modes' `run_async`, mirroring
    `agent_sdk._Outcome`.
    """

    answer: str
    evidence: list[str]
    confidence: _Confidence
    tool_calls: list[ToolCall]
    tokens_in: int
    tokens_out: int
    cost_eur: float


class _Paused(Exception):
    """Raised by `langgraph_multi.run_multi_async`/`resume_multi_async` when
    the graph reaches `_confirm_flag_node` and calls `interrupt()` - not an
    error: decompose, workers, synthesize, and critic already ran to
    completion, and `outcome` carries what they produced rather than
    discarding it, the same "reuse `AgentResult.error` for ended
    early/differently than a clean success" precedent `_BudgetExceeded`
    already established. `thread_id` is what `LangGraphRuntime.resume` needs
    to continue this exact run.
    """

    def __init__(self, thread_id: str, outcome: _Outcome) -> None:
        super().__init__(
            f"paused for review-flag confirmation (thread_id={thread_id!r}) - "
            "call LangGraphRuntime.resume(case, thread_id, approved=...) to continue"
        )
        self.thread_id = thread_id
        self.outcome = outcome


def _messages_telemetry(state: dict[str, Any]) -> tuple[list[ToolCall], int, int]:
    """Default `telemetry_fn` for `_run_graph`: single mode's `create_react_agent`
    graph carries everything in one `state["messages"]` list.
    """
    tool_calls = _extract_tool_calls(state["messages"])
    tokens_in, tokens_out = _sum_usage(state["messages"])
    return tool_calls, tokens_in, tokens_out


async def _run_graph(
    graph: Any,
    case: Case,
    max_turns: int,
    max_wall_clock_s: float,
    max_tool_calls: int,
    *,
    input_state: Any = None,
    telemetry_fn: Callable[
        [dict[str, Any]], tuple[list[ToolCall], int, int]
    ] = _messages_telemetry,
    thread_id: str | None = None,
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

    Single mode (this module) drives with `case.question` as the only input
    and reads telemetry off `state["messages"]` - both are this function's
    defaults, so its own call site doesn't need to change. Multi mode
    (`langgraph_multi.py`) passes its own `AgentState` as `input_state` (or a
    `langgraph.types.Command` to resume a paused run), reads telemetry off
    its own `tool_calls`/`tokens_in`/`tokens_out` reducer fields via a custom
    `telemetry_fn`, and passes `thread_id` explicitly since a resume's
    thread wasn't necessarily created from `case.case_id` in this call
    (`LangGraphRuntime.resume` takes `thread_id` as its own argument).
    """
    config = {
        "configurable": {"thread_id": thread_id or case.case_id},
        # Graph *steps*, not literal turns (each tool-call round trip is ~2
        # steps in this prebuilt graph) - reuses investigator.max_turns as a
        # generous, not exact, bound, rather than forking a second
        # turn-budget config.
        "recursion_limit": max_turns,
    }
    if input_state is None:
        input_state = {"messages": [("user", case.question)]}

    # Written from inside `_drive` on every step, read from the `TimeoutError`
    # handler below. A wall-clock breach cancels `_drive`'s task, but this
    # variable lives in `_run_graph`'s own frame, not the cancelled task's -
    # it still holds whatever the last completed step wrote.
    last_state: dict[str, Any] | None = None

    async def _drive() -> dict[str, Any]:
        nonlocal last_state
        stream = graph.astream(
            input_state,
            config=config,
            stream_mode="values",
        )
        async with contextlib.aclosing(stream):
            async for chunk in stream:
                last_state = chunk
                tool_calls, tokens_in, tokens_out = telemetry_fn(chunk)
                if len(tool_calls) >= max_tool_calls:
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
        reason = f"wall-clock budget of {max_wall_clock_s}s exceeded"
        if isinstance(exc, BaseExceptionGroup):
            cancelled, other = exc.split((asyncio.CancelledError, TimeoutError))
            if cancelled is None:
                raise
            if other is not None:
                # A real cancellation happened, but something else broke at
                # the same time - graceful degradation for the cancellation
                # still applies (below), but that other failure must not be
                # silently dropped just because it arrived bundled with it.
                reason = f"{reason}; also: {other!r}"
        tool_calls, tokens_in, tokens_out = (
            telemetry_fn(last_state) if last_state else ([], 0, 0)
        )
        raise _BudgetExceeded(
            reason,
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


async def _build_react_subgraph(
    *,
    model_name: str,
    prompt: str,
    response_format: type[BaseModel],
    created_by: str,
    tool_names: tuple[str, ...] | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
) -> Any:
    """Build one `create_react_agent` graph against the project's MCP server:
    spawn the subprocess, fetch its tools, optionally filter them down to
    `tool_names` (ADR-0010's Python-side tool restriction - `None` means
    every tool, single mode's own subset), bind `prompt`/`response_format`.

    Reused for single mode's one investigator (this module, `tool_names=None`,
    a real `checkpointer` since its whole run goes through `_run_graph`'s
    thread-scoped `astream`) and for each multi-mode worker
    (`langgraph_multi.py`, `tool_names` from `config/roles.yaml`,
    `checkpointer=None` - workers are invoked directly with `.ainvoke()`,
    with no thread/interrupt needs of their own).
    """
    env = {**os.environ, "RECON_CREATED_BY": created_by}
    # No explicit teardown here - verified directly against this project's
    # installed langchain-mcp-adapters source, not just its docs (round 3
    # review of PR #46 asked for this): both get_tools()'s discovery call and
    # every individual bound tool's execution (convert_mcp_tool_to_langchain_tool's
    # call_tool) scope their own subprocess session inside `async with
    # create_session(...)`, torn down via the context manager protocol on
    # success, exception, or cancellation alike - MultiServerMCPClient itself
    # never holds a persistent session to close, unlike agent_sdk.py's one
    # long-lived query() stream (contextlib.aclosing in _run_query).
    client = MultiServerMCPClient({MCP_SERVER_NAME: _mcp_connection(env=env)})
    tools = await client.get_tools()
    if tool_names is not None:
        wanted = {f"{name}_tool" for name in tool_names}
        tools = [tool for tool in tools if tool.name in wanted]

    # mypy's stub for ChatAnthropic's generated __init__ doesn't surface
    # `model` as a valid kwarg, though it's a genuine pydantic field
    # (confirmed: ChatAnthropic.model_fields, and constructs fine at
    # runtime) - a stub gap, not a real type error.
    model = ChatAnthropic(model=model_name)  # type: ignore[call-arg]
    return create_react_agent(
        model,
        tools,
        prompt=prompt,
        response_format=response_format,
        checkpointer=checkpointer,
    )


class LangGraphRuntime:
    """`Runtime` implementation driving LangGraph. Single mode (this module's
    own `create_react_agent` graph) and multi mode (`runtimes/
    langgraph_multi.py`'s hand-built `StateGraph`, ADR-0010) share this one
    entry point.
    """

    def __init__(
        self,
        *,
        mode: Literal["single", "multi"] = "single",
        models_config_path: Path = DEFAULT_MODELS_CONFIG_PATH,
        prompt_path: Path = DEFAULT_PROMPT_PATH,
        roles_config_path: Path | None = None,
        prompts_dir: Path | None = None,
    ) -> None:
        self._mode = mode
        self._models_config_path = models_config_path
        self._prompt_path = prompt_path
        # Only meaningful for mode="multi" - None means "use langgraph_multi's
        # own defaults" (mirrors AgentSdkRuntime.__init__'s identical
        # roles_config_path/prompts_dir pattern; not imported at module level
        # for the same reason agent_sdk.py doesn't import multi_agent at
        # module level - see run_async's lazy import below).
        self._roles_config_path = roles_config_path
        self._prompts_dir = prompts_dir
        # Built lazily and reused across every run_async/resume call this
        # instance makes (_get_checkpointer) - required for InMemorySaver
        # (its storage lives only in this one Python object; a fresh one per
        # call would make every interrupt unresumable) and kept for
        # AsyncPostgresSaver too rather than reconnecting per call.
        self._checkpointer: BaseCheckpointSaver | None = None

    async def _get_checkpointer(self) -> BaseCheckpointSaver:
        if self._checkpointer is None:
            self._checkpointer = await _build_checkpointer(
                os.environ.get("DATABASE_URL")
            )
        return self._checkpointer

    def run(self, case: Case) -> AgentResult:
        """Answer `case`. Never raises - see `AgentSdkRuntime.run`'s docstring;
        the `Runtime` contract requires every failure to surface as
        `AgentResult.error` instead.
        """
        return asyncio.run(self.run_async(case))

    async def run_async(self, case: Case) -> AgentResult:
        start = time.monotonic()
        model_config: dict[str, Any] = {}
        model_name = ""
        try:
            model_config = _load_model_config(self._models_config_path)
            run_budget = model_config["run_budget"]
            max_tool_calls = int(run_budget["max_tool_calls"])
            max_tokens = int(run_budget["max_tokens"])
            max_wall_clock_s = float(run_budget["max_wall_clock_s"])

            if self._mode == "multi":
                from recon.runtimes import langgraph_multi

                outcome = await langgraph_multi.run_multi_async(
                    case,
                    checkpointer=await self._get_checkpointer(),
                    model_config=model_config,
                    max_tool_calls=max_tool_calls,
                    max_wall_clock_s=max_wall_clock_s,
                    max_turns=int(model_config["investigator"]["max_turns"]),
                    roles_config_path=self._roles_config_path,
                    prompts_dir=self._prompts_dir,
                )
            else:
                investigator = model_config["investigator"]
                model_name = investigator["model"]
                prompt = self._prompt_path.read_text(encoding="utf-8")
                graph = await _build_react_subgraph(
                    model_name=model_name,
                    prompt=prompt,
                    response_format=AnswerResponse,
                    created_by=_CREATED_BY,
                    checkpointer=await self._get_checkpointer(),
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
                outcome = _Outcome(
                    answer=answer,
                    evidence=evidence,
                    confidence=confidence,
                    tool_calls=tool_calls,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_eur=_compute_cost_eur(
                        model_config, model_name, tokens_in, tokens_out
                    ),
                )

            return AgentResult(
                case_id=case.case_id,
                answer=outcome.answer,
                evidence=outcome.evidence,
                confidence=outcome.confidence,
                tool_calls=outcome.tool_calls,
                runtime=RUNTIME_NAME,
                mode=self._mode,
                tokens_in=outcome.tokens_in,
                tokens_out=outcome.tokens_out,
                cost_eur=outcome.cost_eur,
                elapsed_ms=int((time.monotonic() - start) * 1000),
                error=_budget_breach_note(
                    outcome.tokens_in, outcome.tokens_out, max_tokens
                ),
            )
        except _Paused as exc:
            # Not a failure - decompose, workers, synthesize, and critic
            # already produced a real, complete answer; only the review-flag
            # write is pending a human decision. See _Paused's docstring.
            return AgentResult(
                case_id=case.case_id,
                answer=exc.outcome.answer,
                evidence=exc.outcome.evidence,
                confidence=exc.outcome.confidence,
                tool_calls=exc.outcome.tool_calls,
                runtime=RUNTIME_NAME,
                mode=self._mode,
                tokens_in=exc.outcome.tokens_in,
                tokens_out=exc.outcome.tokens_out,
                cost_eur=exc.outcome.cost_eur,
                elapsed_ms=int((time.monotonic() - start) * 1000),
                error=str(exc),
            )
        except _BudgetExceeded as exc:
            # Single mode computes cost here, from its one well-defined
            # model_name (unchanged from part 1) - multi mode has no single
            # model_name to use here, so langgraph_multi.py computes it
            # itself and carries it on the exception (see _BudgetExceeded's
            # docstring).
            cost_eur = (
                exc.cost_eur
                if self._mode == "multi"
                else _compute_cost_eur(
                    model_config, model_name, exc.tokens_in, exc.tokens_out
                )
            )
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
                cost_eur=cost_eur,
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

    async def resume(self, case: Case, thread_id: str, approved: bool) -> AgentResult:
        """Complete a multi-mode run paused at `_confirm_flag_node`
        (`run_async`'s `AgentResult.error` names the `thread_id` to pass
        here). Outside the `Runtime` protocol - issue #14 part 2 asks for the
        interrupt mechanism only, no CLI wiring yet; a natural, separate
        follow-up. Tests call this directly.

        Only meaningful for `mode="multi"` - single mode never pauses, so
        there is never a `thread_id` to resume.
        """
        start = time.monotonic()
        model_config: dict[str, Any] = {}
        try:
            if self._mode != "multi":
                raise RuntimeError(
                    "LangGraphRuntime.resume is only meaningful for "
                    "mode='multi' - single mode never pauses."
                )
            model_config = _load_model_config(self._models_config_path)
            run_budget = model_config["run_budget"]
            max_tool_calls = int(run_budget["max_tool_calls"])
            max_tokens = int(run_budget["max_tokens"])
            max_wall_clock_s = float(run_budget["max_wall_clock_s"])

            from recon.runtimes import langgraph_multi

            outcome = await langgraph_multi.resume_multi_async(
                case,
                thread_id,
                approved,
                checkpointer=await self._get_checkpointer(),
                model_config=model_config,
                max_tool_calls=max_tool_calls,
                max_wall_clock_s=max_wall_clock_s,
                max_turns=int(model_config["investigator"]["max_turns"]),
                roles_config_path=self._roles_config_path,
                prompts_dir=self._prompts_dir,
            )
            return AgentResult(
                case_id=case.case_id,
                answer=outcome.answer,
                evidence=outcome.evidence,
                confidence=outcome.confidence,
                tool_calls=outcome.tool_calls,
                runtime=RUNTIME_NAME,
                mode=self._mode,
                tokens_in=outcome.tokens_in,
                tokens_out=outcome.tokens_out,
                cost_eur=outcome.cost_eur,
                elapsed_ms=int((time.monotonic() - start) * 1000),
                error=_budget_breach_note(
                    outcome.tokens_in, outcome.tokens_out, max_tokens
                ),
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
                cost_eur=exc.cost_eur,
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
