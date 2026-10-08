"""The model-calling nodes of multi mode's graph (`langgraph_multi.py`):
decompose, one worker per routed subtask, synthesize and critic.
"""

from pathlib import Path
from typing import Any, cast

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.types import Send
from pydantic import BaseModel

from recon.runtimes.answer import validate_answer
from recon.runtimes.api_key import langgraph_api_key
from recon.runtimes.evidence import worker_report
from recon.runtimes.langgraph import RUNTIME_NAME, AnswerResponse, _build_react_subgraph
from recon.runtimes.langgraph_run import _compute_cost_eur
from recon.runtimes.langgraph_state import (
    AgentState,
    CriticResponse,
    DecomposeResponse,
    WorkerResponse,
    _WorkerTask,
)
from recon.runtimes.langgraph_trace import (
    _extract_tool_calls,
    _sum_usage,
    row_index,
    tool_row_records,
)

# See langgraph._CREATED_BY - same purpose, multi mode's value.
_CREATED_BY = f"{RUNTIME_NAME}:multi"


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
    # Without this, LangGraph falls back to its own default recursion limit
    # instead of this role's config/roles.yaml max_turns - the same
    # investigator.max_turns -> recursion_limit mapping _run_graph applies
    # for single mode's own graph, just read from this worker's own role
    # entry rather than the investigator's.
    result = await graph.ainvoke(
        {"messages": [("user", task["instruction"])]},
        config={"recursion_limit": role_config["max_turns"]},
    )
    structured = result["structured_response"]
    if not isinstance(structured, WorkerResponse):
        raise TypeError(
            f"worker {worker!r} produced no structured findings "
            f"(got {type(structured)!r})."
        )
    messages = result["messages"]
    tool_calls = _extract_tool_calls(messages)
    tokens_in, tokens_out = _sum_usage(messages)
    rows = tool_row_records(messages)
    finding = worker_report(
        worker, structured.findings, structured.evidence_refs, row_index(rows)
    )
    return {
        "findings": [finding],
        "rows": rows,
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
    model = ChatAnthropic(model=model_name, api_key=langgraph_api_key())  # type: ignore[call-arg]
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
    model = ChatAnthropic(model=model_name, api_key=langgraph_api_key())  # type: ignore[call-arg]
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
    answer = validate_answer(parsed.model_dump(), row_index(state["rows"]))
    return {
        "answer": answer.answer,
        "evidence": answer.evidence,
        "claims": [claim.model_dump(mode="json") for claim in answer.claims],
        "evidence_items": [
            item.model_dump(mode="json") for item in answer.evidence_items
        ],
        "confidence": answer.confidence,
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
    model = ChatAnthropic(model=model_name, api_key=langgraph_api_key())  # type: ignore[call-arg]
    parsed, tokens_in, tokens_out = await _structured_call(
        model,
        [
            SystemMessage(content=critic_prompt),
            HumanMessage(
                content=(
                    f"Question: {state['question']}\n\n"
                    f"Proposed answer: {state['answer']}\n\n"
                    "Cited evidence:\n" + "\n".join(state["evidence"]) + "\n\n"
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
