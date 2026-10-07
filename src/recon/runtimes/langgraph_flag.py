"""Multi mode's review-flag pause (`langgraph_multi.py`): the dedicated
`confirm_flag` node, reached only when the supervisor set `flag_reason`. See
`docs/adr/0010-langgraph-runtime.md` for why it's a node, not a bound tool.
"""

import os
import time
from typing import Any

from langgraph.graph import END
from langgraph.types import interrupt

from recon.contracts import ToolCall
from recon.runtimes.langgraph_nodes import _CREATED_BY
from recon.runtimes.langgraph_state import AgentState
from recon.tools.review_flag import flag_case_for_review


async def _confirm_flag_node(state: AgentState) -> dict[str, Any]:
    """Reached only when the supervisor set `flag_reason` (the conditional
    edge below). Everything before `interrupt()` is read-only - two
    `flag_case_for_review` calls (`dry_run=True` for the preview, then
    unconfirmed for a `preview_token`) that never touch Postgres - so this
    node is safe to replay from the start if it's ever interrupted more than
    once (`langgraph.types.interrupt`'s "resume re-executes the whole node"
    caveat only bites when something before the interrupt has a side
    effect). The actual write, if approved, happens once, after the human
    decision `interrupt()` returns.
    """
    case_id = state["case_id"]
    reason = state["flag_reason"]
    if reason is None:
        raise RuntimeError(
            "_confirm_flag_node reached with flag_reason=None - "
            "_route_after_critic should have routed to END instead."
        )
    idempotency_key = f"review-{case_id}"
    database_url = os.environ.get("DATABASE_URL")
    arguments = {
        "case_id": case_id,
        "reason": reason,
        "idempotency_key": idempotency_key,
    }

    start = time.monotonic()
    preview_arguments = {**arguments, "dry_run": True}
    preview = await flag_case_for_review(
        database_url, case_id, reason, idempotency_key, _CREATED_BY, dry_run=True
    )
    tool_calls = [
        ToolCall(
            tool="flag_case_for_review",
            arguments=preview_arguments,
            status=preview.status,
            elapsed_ms=int((time.monotonic() - start) * 1000),
        )
    ]

    start = time.monotonic()
    unconfirmed_arguments = {**arguments, "confirmed": False}
    unconfirmed = await flag_case_for_review(
        database_url, case_id, reason, idempotency_key, _CREATED_BY
    )
    tool_calls.append(
        ToolCall(
            tool="flag_case_for_review",
            arguments=unconfirmed_arguments,
            status=unconfirmed.status,
            elapsed_ms=int((time.monotonic() - start) * 1000),
        )
    )

    approved = interrupt(
        {
            "case_id": case_id,
            "reason": reason,
            "preview": preview.model_dump(mode="json"),
        }
    )

    if approved:
        confirmed_arguments = {
            **arguments,
            "confirmed": True,
            "preview_token": unconfirmed.preview_token,
        }
        start = time.monotonic()
        confirmed = await flag_case_for_review(
            database_url,
            case_id,
            reason,
            idempotency_key,
            _CREATED_BY,
            confirmed=True,
            preview_token=unconfirmed.preview_token,
        )
        tool_calls.append(
            ToolCall(
                tool="flag_case_for_review",
                arguments=confirmed_arguments,
                status=confirmed.status,
                elapsed_ms=int((time.monotonic() - start) * 1000),
            )
        )

    return {"tool_calls": tool_calls}


def _route_after_critic(state: AgentState) -> str:
    return "confirm_flag" if state.get("flag_reason") else END
