"""Replay a `get_financial_fact` call outside the MCP server, for claim
verification in the harness (#139, ADR 0038).

`AgentResult` keeps each tool call's arguments, not its rows (docs/contracts.md
§4). The fact tool is a plain query over fixed tool data, so running the same
arguments again returns the same rows. They go through `with_refs` exactly as
the server's `get_financial_fact_tool` does, so each row's `ref` is the one the
agent cited.
"""

import logging
from collections.abc import Callable
from typing import Any

import duckdb

from recon.tools.refs import with_refs
from recon.tools.server import get_financial_fact

logger = logging.getLogger(__name__)

TOOL = "get_financial_fact"
_REQUIRED = ("company_id", "concept")
_OPTIONAL = ("fiscal_year", "fiscal_period")


def replay_facts(
    conn: duckdb.DuckDBPyConnection,
) -> Callable[[dict[str, Any]], list[dict[str, Any]]]:
    """A function from one recorded call's arguments to the rows, with refs,
    that call returns against `conn`. A call the tool refuses returns no rows;
    an unavailable database is logged, since every claim then goes unsupported."""

    def facts(arguments: dict[str, Any]) -> list[dict[str, Any]]:
        if any(not isinstance(arguments.get(key), str) for key in _REQUIRED):
            return []
        optional = {key: arguments[key] for key in _OPTIONAL if key in arguments}
        company_id = arguments["company_id"]
        result = get_financial_fact(conn, company_id, arguments["concept"], **optional)
        if result.status == "unavailable":
            logger.warning(
                "replaying %s(%s) failed: %s. Claims citing it go unsupported; "
                "check the tool data the run used.",
                TOOL,
                arguments,
                result.message,
            )
        payload = with_refs(TOOL, result, scope=company_id)
        return list(payload["data"])

    return facts
