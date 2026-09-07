"""Request/response models for the API. `docs/contracts.md` section 5.

`POST /investigate`'s response is `AgentResult` itself (`recon.contracts`) — the
contract already defines that exact shape, so nothing new is needed for it.
"""

from typing import Any

from pydantic import BaseModel, Field


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
