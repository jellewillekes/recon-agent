"""FastAPI service. `docs/contracts.md` section 5: `POST /investigate`,
`GET /capabilities`, the saved runs under `/runs`, `GET /healthz`,
`GET /readyz`, `GET /metrics`.

Twelve-factor: every deployment-varying value is read from an environment
variable once at import time — `RECON_API_MAX_CONCURRENCY`,
`RECON_API_REQUEST_TIMEOUT_S`, `DATABASE_URL`. No hardcoded paths.
"""

import asyncio
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any, Literal

import asyncpg
from fastapi import FastAPI, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.staticfiles import StaticFiles

from recon.api import run_store
from recon.api.health import check_mcp_server, check_postgres
from recon.api.metrics import (
    INVESTIGATE_IN_FLIGHT,
    INVESTIGATE_LATENCY,
    REQUEST_COUNT,
)
from recon.api.schemas import (
    Capabilities,
    DataSource,
    Feedback,
    InvestigateRequest,
    ResearchRun,
    RunList,
    RuntimeCapability,
)
from recon.contracts import AgentResult, Case
from recon.runtimes import api_key
from recon.runtimes.agent_sdk import AgentSdkRuntime
from recon.tools.data_source import tool_data_snapshot_id
from recon.tracing import (
    configure_tracing,
    record_agent_result,
    shutdown_tracing,
    span,
)

logger = logging.getLogger(__name__)

MAX_CONCURRENCY = int(os.environ.get("RECON_API_MAX_CONCURRENCY", "4"))
REQUEST_TIMEOUT_S = float(os.environ.get("RECON_API_REQUEST_TIMEOUT_S", "270"))
DATABASE_URL = os.environ.get("DATABASE_URL")

# Internal safety valve for how long a single /readyz dependency check may
# take, not a deployment-varying value, so a constant rather than an env var.
READYZ_CHECK_TIMEOUT_S = 5.0

# Only what this build actually implements; anything else comes back as 422,
# not silently ignored. LangGraph needs a metered API key (ADR 0027), which
# the service never uses.
SUPPORTED_MODES: tuple[Literal["single", "multi"], ...] = ("single", "multi")
SUPPORTED_RUNTIMES = {"agent_sdk"}
_LANGGRAPH_REASON = (
    "LangGraph needs a metered Anthropic API key (ADR 0027); this service runs "
    "on the Agent SDK subscription only."
)
_NO_RUN_HISTORY = (
    "Run history needs Postgres. Set DATABASE_URL for the API and restart it."
)

REQUEST_ID_HEADER = "X-Request-ID"

_runtimes = {mode: AgentSdkRuntime(mode=mode) for mode in SUPPORTED_MODES}
_semaphore = asyncio.Semaphore(MAX_CONCURRENCY)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # The service runs the Agent SDK on the subscription. With the API key
    # exported, every request would bill the API instead (ADR 0027).
    key_problem = api_key.sdk_key_problem()
    if key_problem is not None:
        raise RuntimeError(key_problem)
    # Started here, not at import: tests import this module without starting it.
    configure_tracing("recon-api")
    yield
    shutdown_tracing()


app = FastAPI(title="recon-agent API", lifespan=_lifespan)


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
        # The request ID is the correlation key between this span, the
        # agent span under it and the API's logs.
        with span(
            f"{request.method} {request.url.path}",
            **{
                "http.request.method": request.method,
                "url.path": request.url.path,
                "recon.request_id": request_id,
            },
        ) as request_span:
            response = await call_next(request)
            request_span.set_attribute(
                "http.response.status_code", response.status_code
            )
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
                with span("invoke_agent") as agent_span:
                    result = await asyncio.wait_for(
                        _runtimes[body.mode or "single"].run_async(case),
                        timeout=REQUEST_TIMEOUT_S,
                    )
                    record_agent_result(agent_span, result)
            finally:
                INVESTIGATE_IN_FLIGHT.dec()
    except TimeoutError:
        # run_async is a real coroutine (unlike run(), which bridges through
        # its own asyncio.run() in a worker thread), so this cancellation
        # actually reaches query() and the SDK's own shielded subprocess
        # teardown - see docs/adr/0006-api-timeout-cancellation-fixed.md.
        # wait_for waits for that teardown to finish (bounded ~20s worst
        # case by the SDK) before raising, so this response can be delayed
        # by cleanup - a deliberate tradeoff over an instant 504 that leaves
        # an orphaned run behind.
        logger.warning(
            "request_id=%s timed out after %.1fs; the run was cancelled and "
            "its subprocess torn down (case_id=%s)",
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

    await _save(body, result)
    REQUEST_COUNT.labels(route="/investigate", status="200").inc()
    return JSONResponse(status_code=status.HTTP_200_OK, content=result.model_dump())


@cache
def _data_source() -> tuple[str, str]:
    """The tools' data kind and snapshot id. Hashing the snapshot reads its
    files, so it's done once per process; the snapshot doesn't change under
    a running server."""
    snapshot = tool_data_snapshot_id()
    return ("fixture" if snapshot.startswith("fixture") else "edgar"), snapshot


async def _save(body: InvestigateRequest, result: AgentResult) -> None:
    """Save a finished run. A failed save is logged, not raised: the answer
    the user waited for matters more than its history entry."""
    if DATABASE_URL is None:
        return
    run = ResearchRun(
        run_id=result.case_id,
        created_at=datetime.now(UTC),
        question=body.question,
        context=body.context,
        runtime=result.runtime,
        mode=result.mode,
        data_source=_data_source()[1],
        result=result,
    )
    try:
        await run_store.save_run(DATABASE_URL, run)
    except (OSError, asyncpg.PostgresError) as exc:
        logger.warning(
            "run_id=%s wasn't saved to the run store (%s); check Postgres "
            "and DATABASE_URL",
            run.run_id,
            type(exc).__name__,
        )


def _no_run_history() -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": _NO_RUN_HISTORY},
    )


def _no_such_run(run_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": f"No saved run {run_id!r}. List saved runs at /runs."},
    )


@app.get("/capabilities")
async def capabilities() -> Capabilities:
    """What this deployment can run, so a client offers only that."""
    kind, snapshot = _data_source()
    return Capabilities(
        runtimes=[
            RuntimeCapability(
                name="agent_sdk", modes=list(SUPPORTED_MODES), supported=True
            ),
            RuntimeCapability(
                name="langgraph",
                modes=list(SUPPORTED_MODES),
                supported=False,
                reason=_LANGGRAPH_REASON,
            ),
        ],
        default_runtime="agent_sdk",
        default_mode="single",
        data_source=DataSource(kind=kind, snapshot=snapshot),  # type: ignore[arg-type]
        run_history=DATABASE_URL is not None,
    )


@app.get("/runs", response_model=None)
async def runs(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> RunList | JSONResponse:
    """Saved runs, newest first."""
    if DATABASE_URL is None:
        return _no_run_history()
    summaries = await run_store.list_runs(DATABASE_URL, limit, offset)
    return RunList(runs=summaries, limit=limit, offset=offset)


@app.get("/runs/{run_id}", response_model=None)
async def get_run(run_id: str) -> ResearchRun | JSONResponse:
    """One saved run: the question, settings, answer, claims, evidence and
    tool calls."""
    if DATABASE_URL is None:
        return _no_run_history()
    run = await run_store.get_run(DATABASE_URL, run_id)
    return _no_such_run(run_id) if run is None else run


@app.get("/runs/{run_id}/export", response_model=None)
async def export_run(run_id: str) -> Response:
    """A saved run as a JSON file to download."""
    if DATABASE_URL is None:
        return _no_run_history()
    run = await run_store.get_run(DATABASE_URL, run_id)
    if run is None:
        return _no_such_run(run_id)
    safe_id = "".join(c for c in run_id if c.isalnum() or c in "-_")
    return Response(
        content=run.model_dump_json(indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="recon-run-{safe_id}.json"'
        },
    )


@app.post("/runs/{run_id}/feedback", response_model=None)
async def run_feedback(
    run_id: str, feedback: Feedback
) -> dict[str, str] | JSONResponse:
    """A 👍/👎 and an optional note on a saved run, replacing any earlier one."""
    if DATABASE_URL is None:
        return _no_run_history()
    if not await run_store.set_feedback(DATABASE_URL, run_id, feedback):
        return _no_such_run(run_id)
    return {"status": "ok"}


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


app.mount(
    "/",
    StaticFiles(directory=Path(__file__).parent / "static", html=True),
    name="web",
)
