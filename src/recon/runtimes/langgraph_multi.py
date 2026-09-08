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

import operator
import os
import time
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict, cast

import yaml
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, Send, interrupt
from pydantic import BaseModel, Field

from recon.contracts import Case, ToolCall
from recon.runtimes.langgraph import (
    RUNTIME_NAME,
    AnswerResponse,
    _BudgetExceeded,
    _build_react_subgraph,
    _compute_cost_eur,
    _Confidence,
    _extract_tool_calls,
    _Outcome,
    _Paused,
    _run_graph,
    _sum_usage,
)
from recon.tools.review_flag import flag_case_for_review

DEFAULT_ROLES_CONFIG_PATH = Path("config/roles.yaml")
DEFAULT_PROMPTS_DIR = Path("prompts")

WORKER_NAMES = ("worker_lookup", "worker_facts")

# See langgraph._CREATED_BY - same purpose, multi mode's value.
_CREATED_BY = f"{RUNTIME_NAME}:multi"


class _Subtask(BaseModel):
    worker: Literal["worker_lookup", "worker_facts"]
    instruction: str


class DecomposeResponse(BaseModel):
    """Mirrors `multi_agent._DECOMPOSE_SCHEMA`'s shape."""

    subtasks: list[_Subtask] = Field(min_length=1, max_length=4)


class WorkerResponse(BaseModel):
    """Mirrors `multi_agent._WORKER_SCHEMA`'s shape."""

    findings: str
    evidence: list[str]


class CriticResponse(BaseModel):
    """Mirrors `multi_agent._CRITIC_SCHEMA`'s shape."""

    accepted: bool
    reason: str


class AgentState(TypedDict):
    """The multi-mode graph's shared state. `findings`/`tool_calls`/
    `tokens_in`/`tokens_out`/`cost_eur` use reducers so parallel worker
    branches (fanned out via `Send`) merge cleanly instead of one branch's
    write clobbering another's.
    """

    case_id: str
    question: str
    subtasks: list[dict[str, str]]
    findings: Annotated[list[str], operator.add]
    tool_calls: Annotated[list[ToolCall], operator.add]
    tokens_in: Annotated[int, operator.add]
    tokens_out: Annotated[int, operator.add]
    cost_eur: Annotated[float, operator.add]
    answer: str
    evidence: list[str]
    confidence: _Confidence
    flag_reason: str | None


class _WorkerTask(TypedDict):
    """`Send`'s own `arg` for one worker node invocation - deliberately not
    `AgentState`: `Send` lets a target node's input differ from the main
    graph state (see `langgraph.types.Send`'s docstring), and a worker only
    ever needs these three fields.
    """

    case_id: str
    worker: str
    instruction: str


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


async def _structured_call(
    model: ChatAnthropic, messages: list[Any], schema: type[BaseModel]
) -> tuple[BaseModel, int, int]:
    """The decompose/synthesize/critic primitive: one non-tool-calling model
    call producing schema-validated structured output - the same
    `with_structured_output` primitive `create_react_agent` already uses
    internally for the final-answer step (`langgraph.py`'s single mode),
    called directly here since there's no tool loop to wrap it in
    (`config/roles.yaml`'s supervisor/critic entries have no `tools:` key -
    this module never binds `flag_case_for_review` as a tool at all, see
    this module's docstring).

    `include_raw=True` is what surfaces `AIMessage.usage_metadata` - without
    it, `with_structured_output` returns only the parsed schema instance and
    every decompose/synthesize/critic call would be invisible to
    `AgentResult.tokens_in`/`tokens_out`.
    """
    structured_model = model.with_structured_output(schema, include_raw=True)
    # with_structured_output's return-type stub doesn't vary on include_raw
    # (always dict[Any, Any] | BaseModel) - a stub gap, not a real type
    # error: include_raw=True always returns the {'raw','parsed',
    # 'parsing_error'} dict shape, confirmed against langchain_core's own
    # docstring for this method.
    result = cast("dict[str, Any]", await structured_model.ainvoke(messages))
    parsed = result["parsed"]
    if parsed is None:
        raise TypeError(
            f"structured call for {schema.__name__} produced no parsed output "
            f"(parsing_error={result['parsing_error']!r})."
        )
    raw = result["raw"]
    tokens_in = tokens_out = 0
    if isinstance(raw, AIMessage) and raw.usage_metadata:
        tokens_in = raw.usage_metadata.get("input_tokens", 0)
        tokens_out = raw.usage_metadata.get("output_tokens", 0)
    return parsed, tokens_in, tokens_out


async def _run_worker_task(
    task: _WorkerTask,
    *,
    roles_config: dict[str, Any],
    model_config: dict[str, Any],
    prompts_dir: Path,
) -> dict[str, Any]:
    """One worker's routed subtask: its own `create_react_agent` subgraph
    (`langgraph.py`'s `_build_react_subgraph`, reused unchanged from single
    mode), restricted to `config/roles.yaml`'s tool subset for that role
    (ADR-0010's Python-side filter). Invoked directly with `.ainvoke()`, not
    through `_run_graph` - no thread/interrupt needs of its own, so no
    checkpointer either.
    """
    worker = task["worker"]
    role_config = roles_config[worker]
    model_name = role_config["model"]
    prompt = (prompts_dir / f"{worker}.md").read_text(encoding="utf-8")
    graph = await _build_react_subgraph(
        model_name=model_name,
        prompt=prompt,
        response_format=WorkerResponse,
        created_by=_CREATED_BY,
        tool_names=tuple(role_config["tools"]),
    )
    result = await graph.ainvoke({"messages": [("user", task["instruction"])]})
    structured = result["structured_response"]
    if not isinstance(structured, WorkerResponse):
        raise TypeError(
            f"worker {worker!r} produced no structured findings "
            f"(got {type(structured)!r})."
        )
    messages = result["messages"]
    tool_calls = _extract_tool_calls(messages)
    tokens_in, tokens_out = _sum_usage(messages)
    finding = (
        f"[{worker}] findings: {structured.findings}\nevidence: {structured.evidence}"
    )
    return {
        "findings": [finding],
        "tool_calls": tool_calls,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_eur": _compute_cost_eur(model_config, model_name, tokens_in, tokens_out),
    }


async def _decompose_node(
    state: AgentState,
    *,
    roles_config: dict[str, Any],
    model_config: dict[str, Any],
    supervisor_prompt: str,
) -> dict[str, Any]:
    role_config = roles_config["supervisor"]
    model_name = role_config["model"]
    model = ChatAnthropic(model=model_name)  # type: ignore[call-arg]
    parsed, tokens_in, tokens_out = await _structured_call(
        model,
        [
            SystemMessage(content=supervisor_prompt),
            HumanMessage(
                content=(
                    f"Case ID: {state['case_id']}\n\n"
                    "Decompose this question into subtasks for your workers: "
                    f"{state['question']}"
                )
            ),
        ],
        DecomposeResponse,
    )
    assert isinstance(parsed, DecomposeResponse)
    subtasks = [
        {"worker": subtask.worker, "instruction": subtask.instruction}
        for subtask in parsed.subtasks
    ]
    cost_eur = _compute_cost_eur(model_config, model_name, tokens_in, tokens_out)
    return {
        "subtasks": subtasks,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_eur": cost_eur,
    }


def _route_to_workers(state: AgentState) -> list[Send]:
    return [
        Send(
            "worker",
            _WorkerTask(
                case_id=state["case_id"],
                worker=subtask["worker"],
                instruction=subtask["instruction"],
            ),
        )
        for subtask in state["subtasks"]
    ]


async def _synthesize_node(
    state: AgentState,
    *,
    roles_config: dict[str, Any],
    model_config: dict[str, Any],
    supervisor_prompt: str,
) -> dict[str, Any]:
    role_config = roles_config["supervisor"]
    model_name = role_config["model"]
    model = ChatAnthropic(model=model_name)  # type: ignore[call-arg]
    findings_text = "\n\n".join(state["findings"])
    parsed, tokens_in, tokens_out = await _structured_call(
        model,
        [
            SystemMessage(content=supervisor_prompt),
            HumanMessage(
                content=(
                    f"Case ID: {state['case_id']}\n\n"
                    f"Original question: {state['question']}\n\n"
                    f"Worker findings:\n{findings_text}\n\n"
                    "Synthesize a final answer from these findings only."
                )
            ),
        ],
        AnswerResponse,
    )
    assert isinstance(parsed, AnswerResponse)
    cost_eur = _compute_cost_eur(model_config, model_name, tokens_in, tokens_out)
    return {
        "answer": parsed.answer,
        "evidence": parsed.evidence,
        "confidence": parsed.confidence,
        "flag_reason": parsed.flag_reason,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_eur": cost_eur,
    }


async def _critic_node(
    state: AgentState,
    *,
    roles_config: dict[str, Any],
    model_config: dict[str, Any],
    critic_prompt: str,
) -> dict[str, Any]:
    role_config = roles_config["critic"]
    model_name = role_config["model"]
    model = ChatAnthropic(model=model_name)  # type: ignore[call-arg]
    parsed, tokens_in, tokens_out = await _structured_call(
        model,
        [
            SystemMessage(content=critic_prompt),
            HumanMessage(
                content=(
                    f"Question: {state['question']}\n\n"
                    f"Proposed answer: {state['answer']}\n\n"
                    f"Cited evidence: {state['evidence']}\n\n"
                    "Does the evidence support the answer?"
                )
            ),
        ],
        CriticResponse,
    )
    assert isinstance(parsed, CriticResponse)
    confidence = state["confidence"]
    if not parsed.accepted:
        # No retry loop on rejection - matches ADR-0007's no-retry stance,
        # which multi_agent.py's own critic step already carries.
        confidence = "low"
    cost_eur = _compute_cost_eur(model_config, model_name, tokens_in, tokens_out)
    return {
        "confidence": confidence,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_eur": cost_eur,
    }


async def _confirm_flag_node(state: AgentState) -> dict[str, Any]:
    """Reached only when the supervisor set `flag_reason` (the conditional
    edge below). Everything before `interrupt()` is read-only - two
    `flag_case_for_review` calls (`dry_run=True` for the preview, then
    unconfirmed for a `preview_token`) that never touch Postgres - so this
    node is safe to replay from the start if it's ever interrupted more than
    once (`langgraph.types.interrupt`'s "resume re-executes the whole node"
    caveat only bites when something before the interrupt has a side
    effect). The actual write, if approved, happens once, after the human
    decision `interrupt()` returns.
    """
    case_id = state["case_id"]
    reason = state["flag_reason"]
    if reason is None:
        raise RuntimeError(
            "_confirm_flag_node reached with flag_reason=None - "
            "_route_after_critic should have routed to END instead."
        )
    idempotency_key = f"review-{case_id}"
    database_url = os.environ.get("DATABASE_URL")
    arguments = {"case_id": case_id, "reason": reason}

    start = time.monotonic()
    preview = await flag_case_for_review(
        database_url, case_id, reason, idempotency_key, _CREATED_BY, dry_run=True
    )
    tool_calls = [
        ToolCall(
            tool="flag_case_for_review",
            arguments=arguments,
            status=preview.status,
            elapsed_ms=int((time.monotonic() - start) * 1000),
        )
    ]

    start = time.monotonic()
    unconfirmed = await flag_case_for_review(
        database_url, case_id, reason, idempotency_key, _CREATED_BY
    )
    tool_calls.append(
        ToolCall(
            tool="flag_case_for_review",
            arguments=arguments,
            status=unconfirmed.status,
            elapsed_ms=int((time.monotonic() - start) * 1000),
        )
    )

    approved = interrupt(
        {
            "case_id": case_id,
            "reason": reason,
            "preview": preview.model_dump(mode="json"),
        }
    )

    if approved:
        start = time.monotonic()
        confirmed = await flag_case_for_review(
            database_url,
            case_id,
            reason,
            idempotency_key,
            _CREATED_BY,
            confirmed=True,
            preview_token=unconfirmed.preview_token,
        )
        tool_calls.append(
            ToolCall(
                tool="flag_case_for_review",
                arguments=arguments,
                status=confirmed.status,
                elapsed_ms=int((time.monotonic() - start) * 1000),
            )
        )

    return {"tool_calls": tool_calls}


def _route_after_critic(state: AgentState) -> str:
    return "confirm_flag" if state.get("flag_reason") else END


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
        answer="",
        evidence=[],
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

    try:
        result = await _run_graph(
            graph,
            case,
            max_turns,
            max_wall_clock_s,
            max_tool_calls,
            input_state=_initial_state(case),
            telemetry_fn=_state_telemetry,
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
        raise _Paused(case.case_id, _outcome_from_state(result))

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
