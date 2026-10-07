"""Row refs on tool output (ADR 0030, docs/contracts.md section 3)."""

import json
from typing import Any

import duckdb
import pytest

from recon.contracts import ToolResult
from recon.tools import fixtures
from recon.tools.mcp_server import build_server
from recon.tools.refs import row_ref, with_refs

ROW = {"concept": "revenue", "value": 500.0, "fiscal_year": 2024}


@pytest.mark.unit
def test_a_ref_is_E_and_12_hex_characters() -> None:
    ref = row_ref("get_financial_fact", ROW)
    assert ref.startswith("E")
    assert len(ref) == 13
    int(ref[1:], 16)


@pytest.mark.unit
def test_the_same_row_gets_the_same_ref_whatever_its_key_order() -> None:
    reordered = dict(reversed(list(ROW.items())))
    assert row_ref("get_financial_fact", ROW) == row_ref(
        "get_financial_fact", reordered
    )


@pytest.mark.unit
def test_a_different_row_or_tool_gets_a_different_ref() -> None:
    changed = {**ROW, "value": 501.0}
    assert row_ref("get_financial_fact", ROW) != row_ref("get_financial_fact", changed)
    assert row_ref("get_financial_fact", ROW) != row_ref("search_filings", ROW)


@pytest.mark.unit
def test_with_refs_adds_a_ref_to_every_row_and_stays_a_valid_result() -> None:
    result = ToolResult(
        status="ok",
        data=[ROW, {**ROW, "value": 1.0}],
        row_count=2,
        message="2",
        elapsed_ms=1,
    )
    payload = with_refs("get_financial_fact", result)
    refs = [row["ref"] for row in payload["data"]]
    assert refs == [row_ref("get_financial_fact", row) for row in result.data]
    ToolResult.model_validate(payload)


@pytest.mark.unit
def test_with_refs_leaves_an_empty_result_alone() -> None:
    result = ToolResult(
        status="empty", data=[], row_count=0, message="none", elapsed_ms=1
    )
    assert with_refs("get_financial_fact", result) == result.model_dump()


@pytest.mark.unit
def test_with_refs_handles_dates_in_a_row() -> None:
    from datetime import date

    row = {"filed": date(2024, 2, 1)}
    result = ToolResult(status="ok", data=[row], row_count=1, message="1", elapsed_ms=1)
    assert with_refs("search_filings", result)["data"][0]["ref"].startswith("E")


@pytest.mark.unit
@pytest.mark.anyio
async def test_the_mcp_server_returns_rows_with_refs() -> None:
    conn = duckdb.connect(":memory:")
    fixtures.seed(conn)
    server = build_server(conn)
    _, structured = await server.call_tool(
        "get_financial_fact_tool",
        {"company_id": "FIRM-001", "concept": "revenue", "fiscal_year": 2024},
    )
    payload: dict[str, Any] = (
        structured["result"] if "result" in structured else structured
    )
    assert payload["status"] == "ok"
    assert all(row["ref"].startswith("E") for row in payload["data"])
    json.dumps(payload)
