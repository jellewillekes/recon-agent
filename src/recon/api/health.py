"""Dependency checks for `GET /readyz`. `docs/contracts.md` section 5:
"readiness — MCP server reachable AND Postgres reachable."

Kept separate from `main.py` so each check is testable without the ASGI app.
Neither check is used by `/healthz`, which never checks dependencies — a
liveness probe that fails on a database blip restarts a healthy pod.
"""

import asyncio
import sys

import asyncpg
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

MCP_SERVER_COMMAND = [sys.executable, "-m", "recon.tools.mcp_server"]


async def check_mcp_server(timeout_s: float) -> tuple[bool, str]:
    """Spawn the same stdio subprocess `AgentSdkRuntime` uses and complete one
    MCP handshake against it. Reuses the production connection path rather
    than inventing a second way to decide the server is reachable.
    """
    try:
        async with asyncio.timeout(timeout_s):
            params = StdioServerParameters(
                command=MCP_SERVER_COMMAND[0], args=MCP_SERVER_COMMAND[1:]
            )
            async with (
                stdio_client(params) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
        return True, "ok"
    except Exception as exc:  # noqa: BLE001 — boundary: readiness must never raise
        return False, f"MCP server unreachable: {exc}"


async def check_postgres(
    database_url: str | None, timeout_s: float
) -> tuple[bool, str]:
    """`SELECT 1` against `DATABASE_URL`. No table/schema checks — the
    write-path table this will eventually gate on doesn't exist until step 8.
    """
    if not database_url:
        return False, "DATABASE_URL not configured"
    conn = None
    try:
        async with asyncio.timeout(timeout_s):
            conn = await asyncpg.connect(database_url)
            await conn.fetchval("SELECT 1")
        return True, "ok"
    except (TimeoutError, OSError, asyncpg.PostgresError) as exc:
        return False, f"Postgres unreachable: {exc}"
    finally:
        if conn is not None:
            await conn.close()
