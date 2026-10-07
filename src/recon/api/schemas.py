"""Request/response models for the API. `docs/contracts.md` section 5.

`POST /investigate`'s response is `AgentResult` itself (`recon.contracts`) — the
contract already defines that exact shape, so nothing new is needed for it.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from recon.contracts import AgentResult


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
