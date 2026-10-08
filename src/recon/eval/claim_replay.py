"""Verify an answer's claims against the fact rows its agent saw (#139,
ADR 0038).

A stored `AgentResult` keeps each tool call's arguments but not its rows, so
the harness replays the agent's `get_financial_fact` calls, as ADR 0026 does
for searches. Refs are derived from row content (ADR 0030), so a replayed row
carries the ref the agent cited. `verify_claims` then checks each claim
against the rows it cites. No model is called.
"""

from collections.abc import Callable
from typing import Any

from recon.contracts import AgentResult, ClaimVerification
from recon.eval.claim_reader import Row
from recon.eval.verification import verify_claims

FACT_TOOL = "get_financial_fact"

# One recorded call's arguments -> the rows that call returns, each with its
# `ref`. Passed in, like `faithfulness.Passages`, so eval depends on no tool
# module.
Facts = Callable[[dict[str, Any]], list[Row]]


def replayed_rows(agent_result: AgentResult, facts: Facts) -> list[Row]:
    """The rows of every distinct fact call the agent made, one per ref."""
    replayed: list[dict[str, Any]] = []
    rows: dict[str, Row] = {}
    for call in agent_result.tool_calls:
        if call.tool != FACT_TOOL or call.arguments in replayed:
            continue
        replayed.append(call.arguments)
        for row in facts(call.arguments):
            rows.setdefault(str(row.get("ref")), row)
    return list(rows.values())


def verify_answer(agent_result: AgentResult, facts: Facts) -> list[ClaimVerification]:
    """Each claim of the answer, verified against the replayed fact rows.
    Empty when the answer has no claims."""
    if not agent_result.claims:
        return []
    rows = replayed_rows(agent_result, facts)
    return verify_claims(agent_result.claims, rows).claims
