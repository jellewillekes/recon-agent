"""Multi mode's graph state and the structured replies its roles give
(`langgraph_multi.py`). Kept JSON-serializable so a Postgres checkpoint can
hold a paused run.
"""

import operator
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, Field

from recon.contracts import ToolCall
from recon.runtimes.langgraph_run import _Confidence


class _Subtask(BaseModel):
    worker: Literal["worker_lookup", "worker_facts"]
    instruction: str


class DecomposeResponse(BaseModel):
    """Mirrors `multi_agent._DECOMPOSE_SCHEMA`'s shape."""

    subtasks: list[_Subtask] = Field(min_length=1, max_length=4)


class WorkerResponse(BaseModel):
    """Mirrors `multi_agent._WORKER_SCHEMA`'s shape: the findings and the
    refs of the rows they rest on (ADR 0030), as the worker prompts ask."""

    findings: str
    evidence_refs: list[str]


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
    # Every row with a ref the workers' tools returned, as plain JSON records
    # (`langgraph_trace.tool_row_records`), so the synthesis's claims can be
    # resolved against them after a Postgres checkpoint (#118).
    rows: Annotated[list[dict[str, Any]], operator.add]
    answer: str
    # One line per resolved evidence item, as `AgentResult.evidence` has it.
    evidence: list[str]
    # The synthesis's claims and their resolved evidence, as JSON dicts.
    claims: list[dict[str, Any]]
    evidence_items: list[dict[str, Any]]
    confidence: _Confidence
    flag_reason: str | None


class _WorkerTask(TypedDict):  # noqa: PYI049 - used by langgraph_nodes and langgraph_multi
    """`Send`'s own `arg` for one worker node invocation - deliberately not
    `AgentState`: `Send` lets a target node's input differ from the main
    graph state (see `langgraph.types.Send`'s docstring), and a worker only
    ever needs these three fields.
    """

    case_id: str
    worker: str
    instruction: str
