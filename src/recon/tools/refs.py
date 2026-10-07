"""Refs on tool rows, so an answer can cite exactly what a tool returned
(ADR 0030, docs/contracts.md section 3).

A ref is derived from the tool name and the row's content, not its position,
so the same row gets the same ref across multi mode's workers, retries and
replays.
"""

import hashlib
import json
from typing import Any

from recon.contracts import ToolResult


def row_ref(tool: str, row: dict[str, Any]) -> str:
    """`"E"` plus the first 12 hex characters of the sha256 of `tool` and the
    row's canonical JSON. Dates and other non-JSON values hash as strings."""
    canonical = json.dumps(row, sort_keys=True, default=str, separators=(",", ":"))
    digest = hashlib.sha256(f"{tool}\n{canonical}".encode()).hexdigest()
    return f"E{digest[:12]}"


def with_refs(tool: str, result: ToolResult) -> dict[str, Any]:
    """`result` as the dict the MCP server returns, with a `ref` on every row."""
    payload = result.model_dump()
    payload["data"] = [
        {**row, "ref": row_ref(tool, original)}
        for row, original in zip(payload["data"], result.data, strict=True)
    ]
    return payload
