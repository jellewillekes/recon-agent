"""FastAPI service. `docs/contracts.md` section 5: `POST /investigate`,
`GET /healthz`, `GET /readyz`, `GET /metrics`.

Twelve-factor: every deployment-varying value is read from an environment
variable once at import time — `RECON_API_MAX_CONCURRENCY`,
`RECON_API_REQUEST_TIMEOUT_S`, `DATABASE_URL`. No hardcoded paths.
"""

import asyncio
import logging
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from recon.api.health import check_mcp_server, check_postgres
from recon.api.metrics import (
    INVESTIGATE_IN_FLIGHT,
    INVESTIGATE_LATENCY,
    REQUEST_COUNT,
)
from recon.api.schemas import InvestigateRequest
from recon.contracts import Case
from recon.runtimes.agent_sdk import AgentSdkRuntime

logger = logging.getLogger(__name__)

MAX_CONCURRENCY = int(os.environ.get("RECON_API_MAX_CONCURRENCY", "4"))
REQUEST_TIMEOUT_S = float(os.environ.get("RECON_API_REQUEST_TIMEOUT_S", "120"))
DATABASE_URL = os.environ.get("DATABASE_URL")

# Internal safety valve for how long a single /readyz dependency check may
# take, not a deployment-varying value, so a constant rather than an env var.
READYZ_CHECK_TIMEOUT_S = 5.0

# Only what this build actually implements. mode="multi" is step 7; a second
# runtime is later — both come back as 422 until then, not silently ignored.
SUPPORTED_MODES = {"single"}
SUPPORTED_RUNTIMES = {"agent_sdk"}

REQUEST_ID_HEADER = "X-Request-ID"

_runtime = AgentSdkRuntime()
_semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

app = FastAPI(title="recon-agent API")


class _RequestIDMiddleware(BaseHTTPMiddleware):
    """Accepts an inbound `X-Request-ID` or generates one, and echoes it back
    on every response — including ones produced by exception handlers, since
    `request.state` is shared across the whole call chain via the ASGI scope.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


app.add_middleware(_RequestIDMiddleware)


@app.exception_handler(RequestValidationError)
async def _on_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """400, "invalid input, with the offending field named" — for a body that
    fails schema validation itself (missing or mistyped fields). FastAPI's
    own default for this case is 422; `investigate` below uses 422 instead
    for a schema-valid body this build can't process.
    """
    fields = [".".join(str(p) for p in err["loc"][1:]) for err in exc.errors()]
    REQUEST_COUNT.labels(route=request.url.path, status="400").inc()
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": "invalid input", "fields": fields},
    )


def _build_case(request_id: str, body: InvestigateRequest) -> Case:
    """A live request has no expected answer to score against. `""` (not
    `None`) satisfies `Case`'s validator, which only requires the field be
    present — it does not fabricate content.
    """
    return Case(
        case_id=request_id,
        source="api",
        question=body.question,
        expected_answer="",
        expected_tool_path=None,
        context=body.context,
        tags=[],
        license="internal",
        attribution="live API request",
    )


def _unprocessable(field: str, message: str) -> JSONResponse:
    REQUEST_COUNT.labels(route="/investigate", status="422").inc()
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"detail": message, "field": field},
    )


@app.post("/investigate")
async def investigate(request: Request, body: InvestigateRequest) -> Response:
    request_id: str = request.state.request_id

    if not body.question.strip():
        return _unprocessable("question", "question must not be empty")
    if body.mode is not None and body.mode not in SUPPORTED_MODES:
        return _unprocessable("mode", f"unsupported mode {body.mode!r}")
    if body.runtime is not None and body.runtime not in SUPPORTED_RUNTIMES:
        return _unprocessable("runtime", f"unsupported runtime {body.runtime!r}")

    case = _build_case(request_id, body)
    start = time.monotonic()
    try:
        async with _semaphore:
            # Counted only once a concurrency slot is held, matching
            # metrics.py's "requests currently running" — not requests
            # still queued behind the semaphore.
            INVESTIGATE_IN_FLIGHT.inc()
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(_runtime.run, case), timeout=REQUEST_TIMEOUT_S
                )
            finally:
                INVESTIGATE_IN_FLIGHT.dec()
    except TimeoutError:
        # wait_for only cancels the await, not _runtime.run's underlying
        # thread/subprocess (see docs/adr/0005-api-timeout-cancellation-deferred.md) -
        # the run keeps executing after this response goes out, so log it as
        # the operational signal that a real fix still needs.
        logger.warning(
            "request_id=%s timed out after %.1fs; the underlying run may "
            "still be executing in the background (case_id=%s)",
            request_id,
            REQUEST_TIMEOUT_S,
            case.case_id,
        )
        REQUEST_COUNT.labels(route="/investigate", status="504").inc()
        return JSONResponse(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            content={"detail": f"run exceeded the {REQUEST_TIMEOUT_S}s timeout"},
        )
    finally:
        INVESTIGATE_LATENCY.observe(time.monotonic() - start)

    REQUEST_COUNT.labels(route="/investigate", status="200").inc()
    return JSONResponse(status_code=status.HTTP_200_OK, content=result.model_dump())


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness only. Never checks a dependency — a probe that fails on a
    database blip restarts a healthy pod.
    """
    return {"status": "ok"}


@app.get("/readyz")
async def readyz(response: Response) -> dict[str, Any]:
    (mcp_ok, mcp_detail), (pg_ok, pg_detail) = await asyncio.gather(
        check_mcp_server(READYZ_CHECK_TIMEOUT_S),
        check_postgres(DATABASE_URL, READYZ_CHECK_TIMEOUT_S),
    )
    checks = {"mcp_server": mcp_detail, "postgres": pg_detail}
    if mcp_ok and pg_ok:
        return {"status": "ok", "checks": checks}
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "not_ready", "checks": checks}


@app.get("/metrics")
async def metrics() -> PlainTextResponse:
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)
