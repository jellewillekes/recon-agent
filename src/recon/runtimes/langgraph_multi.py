"""Multi mode for `LangGraphRuntime` (`--mode multi`, issue #14 part 2): a
hand-built `StateGraph` - supervisor decomposes, workers run their routed
subtasks in parallel (`langgraph.types.Send`), supervisor synthesizes, a
critic checks the synthesis, and a dedicated `confirm_flag` node - reached
only when the supervisor set `flag_reason` - pauses via `interrupt()` for a
human decision before any write.

See `docs/adr/0010-langgraph-runtime.md` for why `flag_case_for_review` is a
dedicated graph node rather than a bound tool in the supervisor's own
tool-calling loop (unlike `runtimes/multi_agent.py`'s SDK-driven equivalent),
and why the checkpointer falls back to `InMemorySaver` when `DATABASE_URL`
isn't configured. Same module split as `agent_sdk.py`/`multi_agent.py`
(ADR-0007): shared streaming/budget/cost primitives live in `langgraph.py`,
imported here rather than duplicated.
"""

from pathlib import Path
from typing import Any

import yaml
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from recon.contracts import Case, Claim, Evidence, ToolCall
from recon.runtimes.langgraph_flag import _confirm_flag_node, _route_after_critic
from recon.runtimes.langgraph_nodes import (
    _critic_node,
    _decompose_node,
    _route_to_workers,
    _run_worker_task,
    _synthesize_node,
)
from recon.runtimes.langgraph_run import (
    _BudgetExceeded,
    _compute_cost_eur,
    _Outcome,
    _Paused,
    _run_graph,
    new_thread_id,
)
from recon.runtimes.langgraph_state import (
    AgentState,
    CriticResponse,
    DecomposeResponse,
    WorkerResponse,
    _Subtask,
    _WorkerTask,
)

# Re-exported: the graph's schemas belong to this module's public surface.
__all__ = [
    "AgentState",
    "CriticResponse",
    "DecomposeResponse",
    "WorkerResponse",
    "_Subtask",
    "_WorkerTask",
    "resume_multi_async",
    "run_multi_async",
]

DEFAULT_ROLES_CONFIG_PATH = Path("config/roles.yaml")
DEFAULT_PROMPTS_DIR = Path("prompts")


def _load_roles_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        config: dict[str, Any] = yaml.safe_load(f)
    return config


def _state_telemetry(state: dict[str, Any]) -> tuple[list[ToolCall], int, int]:
    """`_run_graph`'s `telemetry_fn` for this module's graph: unlike single
    mode's one `messages` list, `AgentState` already tracks running totals
    itself (the reducers above), so this just reads them directly.
    """
    return (
        state.get("tool_calls", []),
        state.get("tokens_in", 0),
        state.get("tokens_out", 0),
    )


def _build_multi_graph(
    roles_config: dict[str, Any],
    model_config: dict[str, Any],
    prompts_dir: Path,
    checkpointer: BaseCheckpointSaver,
) -> CompiledStateGraph:
    supervisor_prompt = (prompts_dir / "supervisor_langgraph.md").read_text(
        encoding="utf-8"
    )
    critic_prompt = (prompts_dir / "critic.md").read_text(encoding="utf-8")

    async def decompose(state: AgentState) -> dict[str, Any]:
        return await _decompose_node(
            state,
            roles_config=roles_config,
            model_config=model_config,
            supervisor_prompt=supervisor_prompt,
        )

    async def worker(task: _WorkerTask) -> dict[str, Any]:
        return await _run_worker_task(
            task,
            roles_config=roles_config,
            model_config=model_config,
            prompts_dir=prompts_dir,
        )

    async def synthesize(state: AgentState) -> dict[str, Any]:
        return await _synthesize_node(
            state,
            roles_config=roles_config,
            model_config=model_config,
            supervisor_prompt=supervisor_prompt,
        )

    async def critic(state: AgentState) -> dict[str, Any]:
        return await _critic_node(
            state,
            roles_config=roles_config,
            model_config=model_config,
            critic_prompt=critic_prompt,
        )

    graph = StateGraph(AgentState)
    graph.add_node("decompose", decompose)
    # add_node's stub ties every node to AgentState's own shape - it doesn't
    # model Send's documented ability to invoke a node with a different
    # input type (langgraph.types.Send's own docstring example does exactly
    # this: a node whose state is a single-field TypedDict, not the graph's
    # OverallState). Confirmed live against this project's installed
    # langgraph that a node reached only via Send receives Send's `arg`
    # directly, not a value shaped like the main graph state.
    graph.add_node("worker", worker)  # type: ignore[arg-type]
    graph.add_node("synthesize", synthesize)
    graph.add_node("critic", critic)
    graph.add_node("confirm_flag", _confirm_flag_node)
    graph.add_edge(START, "decompose")
    graph.add_conditional_edges("decompose", _route_to_workers, ["worker"])
    graph.add_edge("worker", "synthesize")
    graph.add_edge("synthesize", "critic")
    graph.add_conditional_edges("critic", _route_after_critic, ["confirm_flag", END])
    graph.add_edge("confirm_flag", END)
    return graph.compile(checkpointer=checkpointer)


def _initial_state(case: Case) -> AgentState:
    return AgentState(
        case_id=case.case_id,
        question=case.question,
        subtasks=[],
        findings=[],
        tool_calls=[],
        tokens_in=0,
        tokens_out=0,
        cost_eur=0.0,
        rows=[],
        answer="",
        evidence=[],
        claims=[],
        evidence_items=[],
        confidence="low",
        flag_reason=None,
    )


def _outcome_from_state(state: dict[str, Any]) -> _Outcome:
    return _Outcome(
        answer=state.get("answer", ""),
        evidence=state.get("evidence", []),
        confidence=state.get("confidence", "low"),
        tool_calls=state.get("tool_calls", []),
        tokens_in=state.get("tokens_in", 0),
        tokens_out=state.get("tokens_out", 0),
        cost_eur=state.get("cost_eur", 0.0),
        claims=[Claim.model_validate(c) for c in state.get("claims", [])],
        evidence_items=[
            Evidence.model_validate(e) for e in state.get("evidence_items", [])
        ],
    )


async def run_multi_async(
    case: Case,
    *,
    checkpointer: BaseCheckpointSaver,
    model_config: dict[str, Any],
    max_tool_calls: int,
    max_wall_clock_s: float,
    max_turns: int,
    roles_config_path: Path | None = None,
    prompts_dir: Path | None = None,
) -> _Outcome:
    """Supervisor decomposes -> workers run their routed subtasks (in
    parallel, `Send`-fanned-out) with their own restricted tool subset ->
    supervisor synthesizes -> critic checks the synthesis, forcing
    `confidence="low"` on rejection -> `confirm_flag`, only if the
    supervisor set `flag_reason`, which pauses (raises `_Paused`) for a
    human decision instead of writing straight away.
    """
    roles_config_path = roles_config_path or DEFAULT_ROLES_CONFIG_PATH
    prompts_dir = prompts_dir or DEFAULT_PROMPTS_DIR
    roles_config = _load_roles_config(roles_config_path)
    graph = _build_multi_graph(roles_config, model_config, prompts_dir, checkpointer)
    thread_id = new_thread_id(case)

    try:
        result = await _run_graph(
            graph,
            case,
            max_turns,
            max_wall_clock_s,
            max_tool_calls,
            input_state=_initial_state(case),
            telemetry_fn=_state_telemetry,
            thread_id=thread_id,
        )
    except _BudgetExceeded as exc:
        # _run_graph doesn't know per-role models - re-raise enriched with a
        # real cost (see _BudgetExceeded's docstring), computed with the
        # supervisor's model as a nominal stand-in: every role currently
        # shares one model in config/roles.yaml, and the tokens themselves
        # (exc.tokens_in/out) already reflect the graph's real,
        # reducer-accumulated total up to the breach.
        cost_eur = _compute_cost_eur(
            model_config,
            roles_config["supervisor"]["model"],
            exc.tokens_in,
            exc.tokens_out,
        )
        raise _BudgetExceeded(
            exc.reason,
            tool_calls=exc.tool_calls,
            tokens_in=exc.tokens_in,
            tokens_out=exc.tokens_out,
            cost_eur=cost_eur,
        ) from exc

    if "__interrupt__" in result:
        raise _Paused(thread_id, _outcome_from_state(result))

    return _outcome_from_state(result)


async def resume_multi_async(
    case: Case,
    thread_id: str,
    approved: bool,
    *,
    checkpointer: BaseCheckpointSaver,
    model_config: dict[str, Any],
    max_tool_calls: int,
    max_wall_clock_s: float,
    max_turns: int,
    roles_config_path: Path | None = None,
    prompts_dir: Path | None = None,
) -> _Outcome:
    """Complete a run paused at `_confirm_flag_node`. `approved=True` writes
    the review flag (`_confirm_flag_node`'s `interrupt()` resumes with this
    value); `approved=False` leaves it unwritten, but the rest of the
    already-produced answer/evidence/confidence is unaffected either way.
    """
    roles_config_path = roles_config_path or DEFAULT_ROLES_CONFIG_PATH
    prompts_dir = prompts_dir or DEFAULT_PROMPTS_DIR
    roles_config = _load_roles_config(roles_config_path)
    graph = _build_multi_graph(roles_config, model_config, prompts_dir, checkpointer)

    try:
        result = await _run_graph(
            graph,
            case,
            max_turns,
            max_wall_clock_s,
            max_tool_calls,
            input_state=Command(resume=approved),
            telemetry_fn=_state_telemetry,
            thread_id=thread_id,
        )
    except _BudgetExceeded as exc:
        cost_eur = _compute_cost_eur(
            model_config,
            roles_config["supervisor"]["model"],
            exc.tokens_in,
            exc.tokens_out,
        )
        raise _BudgetExceeded(
            exc.reason,
            tool_calls=exc.tool_calls,
            tokens_in=exc.tokens_in,
            tokens_out=exc.tokens_out,
            cost_eur=cost_eur,
        ) from exc

    if "__interrupt__" in result:
        # confirm_flag has exactly one interrupt() call, already consumed by
        # this resume - shouldn't happen, but degrade the same way a first
        # pause does rather than silently dropping a second one.
        raise _Paused(thread_id, _outcome_from_state(result))

    return _outcome_from_state(result)
