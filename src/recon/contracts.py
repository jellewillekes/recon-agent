"""Pydantic v2 models for every module boundary.

Kept in exact agreement with `docs/contracts.md` — see `tests/test_contracts.py`
for the test that enforces this. Changing a model's fields is a contract change:
update `docs/contracts.md` in the same PR and state the reason.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class Case(BaseModel):
    """An external benchmark question mapped into our internal schema."""

    case_id: str
    source: str
    question: str
    expected_answer: str | None
    expected_tool_path: list[str] | None
    context: dict[str, Any]
    tags: list[str]
    license: str
    attribution: str

    @model_validator(mode="after")
    def _requires_answer_or_tool_path(self) -> "Case":
        """A case scorable by neither answer nor path is not a valid case."""
        if self.expected_answer is None and self.expected_tool_path is None:
            raise ValueError(
                "Case requires expected_answer or expected_tool_path: a case "
                "with neither is not scorable."
            )
        return self


class ToolResult(BaseModel):
    """What every MCP tool returns. Never a bare list, never None, never an exception."""

    status: Literal["ok", "empty", "truncated", "invalid_input", "unavailable"]
    data: list[dict[str, Any]]
    row_count: int
    message: str
    elapsed_ms: int

    @model_validator(mode="after")
    def _status_matches_data(self) -> "ToolResult":
        """Enforce the per-status data/row_count shape from docs/contracts.md section 3."""
        if not self.message:
            raise ValueError("ToolResult.message must always be populated.")

        if self.status in ("empty", "invalid_input", "unavailable"):
            if self.data:
                raise ValueError(
                    f"ToolResult.data must be empty for status={self.status!r}."
                )
            if self.row_count != 0:
                raise ValueError(
                    f"ToolResult.row_count must be 0 for status={self.status!r}."
                )
        elif self.status == "ok":
            if not self.data:
                raise ValueError("ToolResult.data must be populated for status='ok'.")
            if self.row_count != len(self.data):
                raise ValueError(
                    "ToolResult.row_count must equal len(data) for status='ok'."
                )
        elif self.status == "truncated":
            if not self.data:
                raise ValueError(
                    "ToolResult.data must be populated for status='truncated'."
                )
            if self.row_count <= len(self.data):
                raise ValueError(
                    "ToolResult.row_count must exceed len(data) for status='truncated' "
                    "(it reports how many rows exist beyond what was returned)."
                )
        return self


class ToolCall(BaseModel):
    """One tool invocation, in the order it was made."""

    tool: str
    arguments: dict[str, Any]
    status: str
    elapsed_ms: int


class AgentResult(BaseModel):
    """What every runtime produces, regardless of which one ran the case."""

    case_id: str
    answer: str
    evidence: list[str]
    confidence: Literal["high", "medium", "low"]
    tool_calls: list[ToolCall]
    runtime: str
    mode: Literal["single", "multi"]
    tokens_in: int
    tokens_out: int
    cost_eur: float
    elapsed_ms: int
    error: str | None


class ReviewFlag(BaseModel):
    """The sole write operation: flagging a case for human review."""

    idempotency_key: str
    case_id: str
    reason: str
    created_by: str
    created_at: datetime


class CaseScore(BaseModel):
    """Per-case evaluation result."""

    case_id: str
    task_completion: bool
    answer_score: float = Field(ge=0.0, le=1.0)
    tool_path_exact: bool
    tool_path_equivalent: bool
    tool_call_accuracy: float = Field(ge=0.0, le=1.0)
    rubric_scores: dict[str, float]
    cost_eur: float
    elapsed_ms: int
    notes: str


class EvalRun(BaseModel):
    """A full evaluation run, written to evals/results/<run_id>.json."""

    run_id: str
    timestamp_utc: datetime
    dataset: str
    dataset_license: str
    dataset_attribution: str
    runtime: str
    mode: str
    model_config_hash: str
    prompt_hashes: dict[str, str]
    rubric_version: str
    case_scores: list[CaseScore]
    aggregate: dict[str, float]
    total_cost_eur: float

    @model_validator(mode="after")
    def _prompt_hashes_required(self) -> "EvalRun":
        """Without prompt_hashes a score is not reproducible and the gate can't work."""
        if not self.prompt_hashes:
            raise ValueError("EvalRun.prompt_hashes must not be empty.")
        return self
