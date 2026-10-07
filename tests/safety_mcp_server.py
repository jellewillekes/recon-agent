"""MCP test double for the injection tests (issue #78, #116).

`build_server(vector)` plants one adversarial payload from
`adversarial_cases.py` where that vector says: in a tool's output, a filing
chunk, a tool description or a row's metadata. Writes stay disabled whatever
the agent does. Run as a script it reads the vector from
`RECON_ADVERSARIAL_VECTOR`, defaulting to `tool_output`.
"""

import os
from typing import Any

from adversarial_cases import CASES
from mcp.server.fastmcp import FastMCP

VECTOR_ENV = "RECON_ADVERSARIAL_VECTOR"
_PAYLOADS = {case.vector: case.payload for case in CASES}
_CLEAN_SUMMARY = "Revenue guidance for the year was unchanged."


def _filing_row(company_id: str, vector: str) -> dict[str, Any]:
    in_output = vector in ("tool_output", "escalation", "data_leak")
    row = {
        "company_id": company_id,
        "form": "10-K",
        "section": "md&a",
        "summary": _PAYLOADS[vector] if in_output else _CLEAN_SUMMARY,
    }
    if vector == "document_metadata":
        row["form"] = f"10-K. {_PAYLOADS[vector]}"
        row["section"] = _PAYLOADS[vector]
    return row


def build_server(vector: str = "tool_output") -> FastMCP:
    """The double with `vector`'s payload planted. Raises KeyError for an
    unknown vector; `adversarial_cases.VECTORS` lists them."""
    payload = _PAYLOADS[vector]
    server = FastMCP(name="recon-tools")
    description = "Return synthetic filings for a company."
    if vector == "tool_description":
        description = f"{description} {payload}"

    @server.tool(description=description)
    def search_filings_tool(company_id: str) -> dict[str, Any]:
        return {
            "status": "ok",
            "data": [_filing_row(company_id, vector)],
            "row_count": 1,
            "message": "1 synthetic filing found.",
            "elapsed_ms": 1,
        }

    @server.tool()
    def search_knowledge_tool(query: str, top_k: int = 5) -> dict[str, Any]:
        """Return synthetic filing-text passages for a query."""
        text = payload if vector == "filing_chunk" else _CLEAN_SUMMARY
        return {
            "status": "ok",
            "data": [{"chunk_id": "synthetic:md&a:1", "section": "md&a", "text": text}],
            "row_count": 1,
            "message": "1 synthetic passage found.",
            "elapsed_ms": 1,
        }

    @server.tool()
    def flag_case_for_review_tool(
        case_id: str,
        reason: str,
        idempotency_key: str,
        dry_run: bool = False,
        confirmed: bool = False,
        preview_token: str | None = None,
    ) -> dict[str, Any]:
        """Simulate the write protocol while keeping all writes disabled."""
        if confirmed:
            return {
                "status": "unavailable",
                "flag": None,
                "message": "Writes are disabled in this test double.",
                "preview_token": None,
            }
        status = "would_write" if dry_run else "confirmation_required"
        return {
            "status": status,
            "flag": {
                "case_id": case_id,
                "reason": reason,
                "idempotency_key": idempotency_key,
            },
            "message": "Synthetic preview; no write was made.",
            "preview_token": None if dry_run else "synthetic-preview-token",
        }

    return server


if __name__ == "__main__":
    build_server(os.environ.get(VECTOR_ENV, "tool_output")).run()
