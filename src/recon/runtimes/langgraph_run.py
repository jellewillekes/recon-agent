"""Driving a LangGraph graph within its run budget, and the outcomes a run can
end in. Shared by single and multi mode (`langgraph.py`,
`langgraph_multi.py`). See `docs/adr/0010-langgraph-runtime.md`.
"""

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from recon.contracts import Case, Claim, Evidence, ToolCall
from recon.runtimes.langgraph_trace import _messages_telemetry

_Confidence = Literal["high", "medium", "low"]


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
    either letting a runaway loop keep spending real metered API
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
    # The answer's claims and the evidence their refs resolved to (#118).
    claims: list[Claim] = field(default_factory=list)
    evidence_items: list[Evidence] = field(default_factory=list)


def _failed_outcome(
    tool_calls: list[ToolCall] | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_eur: float = 0.0,
) -> _Outcome:
    """A run that ended without an answer, keeping what it spent."""
    return _Outcome(
        answer="",
        evidence=[],
        confidence="low",
        tool_calls=tool_calls or [],
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_eur=cost_eur,
    )


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


def new_thread_id(case: Case) -> str:
    """A thread of its own for each run. Reducer fields (rows, tool calls,
    tokens) append to whatever a checkpoint already holds, so a rerun on the
    case's id alone would inherit the earlier run's state, and its rows could
    verify the new run's claims (review of #118)."""
    return f"{case.case_id}:{uuid.uuid4().hex[:12]}"


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
        "configurable": {"thread_id": thread_id or new_thread_id(case)},
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
