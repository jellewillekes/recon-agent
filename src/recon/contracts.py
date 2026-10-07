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


class Evidence(BaseModel):
    """One tool row an answer cites, resolved by the server (ADR 0030).

    The model cites a row by its `ref`. `verified` is true only when that ref
    matches a row a tool returned in the same run; the source details and
    `excerpt` then come from that row, never from the model. An unverified
    ref keeps only `ref`, with `source_type="unknown"`.
    """

    ref: str
    verified: bool
    source_type: Literal[
        "filing_text", "financial_fact", "filing", "company", "concept", "unknown"
    ]
    tool: str | None = None
    company_id: str | None = None
    form: str | None = None
    filed: str | None = None
    accession: str | None = None
    section: str | None = None
    locator: str | None = None
    excerpt: str = ""
    retrieval_score: float | None = None
    content_hash: str | None = None


class Claim(BaseModel):
    """A statement in the answer and the evidence refs it rests on."""

    text: str
    importance: Literal["key", "supporting"]
    evidence_refs: list[str]


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
    # ADR 0030. Empty on results recorded before claims existed, and when the
    # model returned no claims; `evidence` is then the model's own strings.
    claims: list[Claim] = []
    evidence_items: list[Evidence] = []


class ReviewFlag(BaseModel):
    """The sole write operation: flagging a case for human review."""

    idempotency_key: str
    case_id: str
    reason: str
    created_by: str
    created_at: datetime


class ReviewFlagResult(BaseModel):
    """What `flag_case_for_review` returns. Not a `ToolResult` — this is a
    state operation, not a read tool (docs/contracts.md section 2), and its
    real states (preview / paused for confirmation / written / already
    written) don't fit ToolResult's five read statuses.

    `preview_token` makes the confirmation pause structurally required, not
    just a prompt convention: it is returned only by the unconfirmed
    ("confirmation_required") call, and a `confirmed=True` call must present
    the matching token or the write is refused. `None` for every other status.
    """

    status: Literal["would_write", "confirmation_required", "created", "already_exists"]
    flag: ReviewFlag | None
    message: str
    preview_token: str | None


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
    # The agent's tool calls by name, in call order (#101). Empty on results
    # recorded before this field existed.
    tool_names: list[str] = []
    # ADR 0030: key claims with a verified ref, and cited refs that are
    # verified. None when there's nothing to score, and on older results.
    claim_support_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    citation_precision: float | None = Field(default=None, ge=0.0, le=1.0)
    # The rubric judge call failed, so answer_score and rubric_scores are
    # placeholders, not measurements (#123).
    judge_failed: bool = False


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
    # "<fetch date>-<content hash>" of the EDGAR tables the tools queried, or
    # "fixture-<hash>". None on runs recorded before this field existed; the
    # promotion gate refuses those.
    tool_data_snapshot: str | None = None
    # Hash of evals/retrieval-labels.yaml when the run has retrieval metrics
    # (#105). Compare those metrics only between runs with the same hash.
    retrieval_labels_hash: str | None = None
    # Whether multi mode's decompose step ran on the local model (step 14,
    # ADR 0029). Recorded, not a comparability field: on against off is the
    # comparison routing is measured by.
    routing: bool = False

    @model_validator(mode="after")
    def _prompt_hashes_required(self) -> "EvalRun":
        """Without prompt_hashes a score is not reproducible and the gate can't work."""
        if not self.prompt_hashes:
            raise ValueError("EvalRun.prompt_hashes must not be empty.")
        return self
