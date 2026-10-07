"""Responses for the run-history endpoints in `main.py` when there's no run
to show: history is off, Postgres is down, or the run doesn't exist."""

import logging
from collections.abc import Awaitable, Callable

import asyncpg
from fastapi import status
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

NO_RUN_HISTORY = (
    "Run history needs Postgres. Set DATABASE_URL for the API and restart it."
)
STORE_DOWN = (
    "Postgres isn't reachable for run history. Check that it's running and that "
    "DATABASE_URL points at it."
)


async def from_store[T](
    database_url: str | None, call: Callable[[str], Awaitable[T]]
) -> T | JSONResponse:
    """Run a run-store call, or a 503 saying why run history isn't available."""
    if database_url is None:
        detail = NO_RUN_HISTORY
    else:
        try:
            return await call(database_url)
        except (OSError, asyncpg.PostgresError) as exc:
            logger.warning("run store unavailable (%s)", type(exc).__name__)
            detail = STORE_DOWN
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"detail": detail}
    )


def no_such_run(run_id: str) -> JSONResponse:
    """404 for a run id the store doesn't have."""
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": f"No saved run {run_id!r}. List saved runs at /runs."},
    )
