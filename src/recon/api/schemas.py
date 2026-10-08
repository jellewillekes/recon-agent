"""Request/response models for the API. `docs/contracts.md` section 5.

`POST /investigate`'s response is `AgentResult` itself (`recon.contracts`) — the
contract already defines that exact shape, so nothing new is needed for it.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from recon.contracts import AgentResult, Claim


class InvestigateRequest(BaseModel):
    """Body of `POST /investigate`.

    `mode`/`runtime` are left as plain strings, not a `Literal`, so an
    unsupported value reaches the endpoint's own check and comes back as a 422
    naming what's unsupported, rather than FastAPI's schema-level 400.
    """

    question: str
    context: dict[str, Any] = Field(default_factory=dict)
    mode: str | None = None
    runtime: str | None = None


MAX_VERIFY_CLAIMS = 100
MAX_VERIFY_ROWS = 1000


class VerifyRequest(BaseModel):
    """Body of `POST /verify`: an answer's claims and the tool rows they rest
    on. A claim that cites refs is checked against those rows only; one that
    cites none, against every row. Rows are `get_financial_fact` output."""

    claims: list[Claim] = Field(default_factory=list, max_length=MAX_VERIFY_CLAIMS)
    evidence: list[dict[str, Any]] = Field(
        default_factory=list, max_length=MAX_VERIFY_ROWS
    )


class Feedback(BaseModel):
    """Body of `POST /runs/{run_id}/feedback`: a human label on an answer,
    the start of the judge-calibration set (#115)."""

    rating: Literal["up", "down"]
    note: str = Field(default="", max_length=2000)


class StoredFeedback(Feedback):
    """Feedback as stored with its run."""

    created_at: datetime


class ResearchRun(BaseModel):
    """One finished `POST /investigate`, as the run store keeps it."""

    run_id: str
    created_at: datetime
    question: str
    context: dict[str, Any]
    runtime: str
    mode: str
    # The tool-data snapshot the run queried, from `/capabilities`.
    data_source: str
    result: AgentResult
    feedback: StoredFeedback | None = None


class RunSummary(BaseModel):
    """One row of `GET /runs`."""

    run_id: str
    created_at: datetime
    question: str
    mode: str
    confidence: str
    cost_eur: float
    elapsed_ms: int
    failed: bool
    claim_count: int
    verified_evidence_count: int
    feedback: Literal["up", "down"] | None


class RunList(BaseModel):
    """Body of `GET /runs`, newest first."""

    runs: list[RunSummary]
    limit: int
    offset: int


class RuntimeCapability(BaseModel):
    """A runtime and whether this deployment can run it."""

    name: str
    modes: list[str]
    supported: bool
    reason: str | None = None


class DataSource(BaseModel):
    """Where the tools' data comes from: SEC EDGAR or synthetic fixtures."""

    kind: Literal["edgar", "fixture"]
    snapshot: str


class Capabilities(BaseModel):
    """Body of `GET /capabilities`."""

    runtimes: list[RuntimeCapability]
    default_runtime: str
    default_mode: str
    data_source: DataSource
    # Whether runs are saved: the run store needs DATABASE_URL.
    run_history: bool


class EvalSummary(BaseModel):
    """One row of `GET /evals`: an eval run from `evals/results/`."""

    run_id: str
    timestamp_utc: datetime
    runtime: str
    mode: str
    routing: bool
    rubric_version: str
    dataset: str
    case_count: int
    task_completion_rate: float | None
    answer_score_mean: float | None
    total_cost_eur: float
    cost_per_correct_answer_eur: float | None
    # What the run left unmeasured (#123): its answer_score_mean then includes
    # placeholder zeros, and the gate refuses it. Empty for a complete run.
    incomplete: list[str] = []


class EvalList(BaseModel):
    """Body of `GET /evals`, newest first."""

    runs: list[EvalSummary]


class Company(BaseModel):
    """A company the tools have data for."""

    company_id: str
    name: str


class CompanyList(BaseModel):
    """Body of `GET /companies`, sorted by ticker."""

    companies: list[Company]
