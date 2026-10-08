"""Tests for `runtimes/langgraph_trace.py`: reading tool calls and the rows
their tools returned out of a LangGraph run's messages (#118). No model, no
MCP server."""

import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from recon.runtimes import langgraph_trace as trace

pytestmark = pytest.mark.unit


def _messages(content: Any, name: str = "list_companies_tool") -> list[BaseMessage]:
    return [
        AIMessage(
            content="",
            tool_calls=[{"name": name, "args": {"query": "FIRM"}, "id": "c1"}],
        ),
        ToolMessage(content=content, tool_call_id="c1"),
    ]


ROW = {
    "ref": "Eabc123",
    "company_id": "FIRM-001",
    "name": "Firm One",
    "sector": "Industrials",
}
PAYLOAD = {
    "status": "ok",
    "data": [ROW, {"no_ref": True}],
    "row_count": 2,
    "message": "m",
    "elapsed_ms": 3,
}


def test_row_records_keep_only_rows_with_a_ref_as_plain_json() -> None:
    blocks = [{"type": "text", "text": json.dumps(PAYLOAD)}]

    records = trace.tool_row_records(_messages(blocks))

    assert records == [
        {"tool": "list_companies", "arguments": {"query": "FIRM"}, "rows": [ROW]}
    ]
    # Plain JSON, so a Postgres checkpoint can hold it.
    assert json.loads(json.dumps(records)) == records


def test_row_records_skip_unparseable_output_and_other_tools() -> None:
    assert trace.tool_row_records(_messages("not json")) == []
    assert (
        trace.tool_row_records(_messages(json.dumps(PAYLOAD), name="AnswerResponse"))
        == []
    )


def test_a_row_index_built_from_records_resolves_their_refs() -> None:
    records = trace.tool_row_records(_messages(json.dumps(PAYLOAD)))

    index = trace.row_index(records)

    assert set(index.rows) == {"Eabc123"}
    assert index.rows["Eabc123"].tool == "list_companies"
