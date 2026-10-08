"""Reading a LangGraph run's messages: the tool calls it made, the tokens it
used, and the rows its tools returned. Shared by single and multi mode
(`langgraph.py`, `langgraph_multi.py`).
"""

import json
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from recon.contracts import ToolCall
from recon.runtimes.evidence import RowIndex

_TOOL_NAMES = (
    "list_companies_tool",
    "list_financial_concepts_tool",
    "get_financial_fact_tool",
    "search_filings_tool",
    # Registered only when a filing-text corpus is built (docs/adr/0025).
    "search_knowledge_tool",
    "flag_case_for_review_tool",
)


def _strip_tool_name(name: str) -> str:
    return name.removesuffix("_tool")


def _tool_message_payload(content: Any) -> dict[str, Any] | None:
    """The JSON our own `ToolResult`/`ReviewFlagResult` produced, from a
    `ToolMessage`'s content. From a real `ToolNode` run that's a list of
    `{"type": "text", "text": "<json>"}` blocks (confirmed live against this
    project's own MCP server). None for anything else."""
    text: str | None = content if isinstance(content, str) else None
    if text is None and isinstance(content, list):
        text = next(
            (
                item.get("text")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ),
            None,
        )
    if text is None:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _parse_tool_message_status(content: Any) -> tuple[str, int]:
    """Pull `status`/`elapsed_ms` back out of a tool's JSON, falling back to
    `("unknown", 0)`, same spirit as `agent_sdk._parse_tool_result`."""
    payload = _tool_message_payload(content)
    if payload is None:
        return "unknown", 0
    status = payload.get("status")
    elapsed_ms = payload.get("elapsed_ms", 0)
    if not isinstance(status, str) or not isinstance(elapsed_ms, int):
        return "unknown", 0
    return status, elapsed_ms


def _answered_calls(
    messages: list[BaseMessage],
) -> list[tuple[str, dict[str, Any], ToolMessage]]:
    """(tool name, arguments, result message) for every call to one of our
    real tools that got a result, in result order."""
    pending: dict[str, tuple[str, dict[str, Any]]] = {}
    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                if call["name"] in _TOOL_NAMES and call["id"] is not None:
                    pending[call["id"]] = (call["name"], call["args"])
    return [
        (*pending[message.tool_call_id], message)
        for message in messages
        if isinstance(message, ToolMessage) and message.tool_call_id in pending
    ]


def tool_row_records(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    """The rows with a `ref` each tool call returned, as plain JSON records
    (`tool`, `arguments`, `rows`), so they survive a Postgres checkpoint in
    multi mode's graph state (#118, ADR 0030)."""
    records: list[dict[str, Any]] = []
    for name, arguments, message in _answered_calls(messages):
        payload = _tool_message_payload(message.content) or {}
        data = payload.get("data")
        rows = [
            row
            for row in (data if isinstance(data, list) else [])
            if isinstance(row, dict) and isinstance(row.get("ref"), str)
        ]
        if rows:
            records.append(
                {
                    "tool": _strip_tool_name(name),
                    "arguments": dict(arguments),
                    "rows": rows,
                }
            )
    return records


def row_index(records: list[dict[str, Any]]) -> RowIndex:
    """A `RowIndex` over `tool_row_records` output, to resolve cited refs."""
    index = RowIndex()
    for record in records:
        index.add(record["tool"], record["arguments"], {"data": record["rows"]})
    return index


def _extract_tool_calls(messages: list[BaseMessage]) -> list[ToolCall]:
    """Walk the graph's final message list pairing each `AIMessage.tool_calls`
    entry with its matching `ToolMessage` by `tool_call_id`. Only tool names
    in `_TOOL_NAMES` count - `response_format`'s own internal structured-
    output tool call (a real message in this list too) is excluded by not
    matching that allowlist, the same "only our real tools count" principle
    as `agent_sdk._run_query`'s `mcp_prefix` filter.
    """
    tool_calls: list[ToolCall] = []
    for name, args, message in _answered_calls(messages):
        status, elapsed_ms = _parse_tool_message_status(message.content)
        tool_calls.append(
            ToolCall(
                tool=_strip_tool_name(name),
                arguments=args,
                status=status,
                elapsed_ms=elapsed_ms,
            )
        )
    return tool_calls


def _sum_usage(messages: list[BaseMessage]) -> tuple[int, int]:
    tokens_in = 0
    tokens_out = 0
    for message in messages:
        if isinstance(message, AIMessage) and message.usage_metadata:
            tokens_in += message.usage_metadata.get("input_tokens", 0)
            tokens_out += message.usage_metadata.get("output_tokens", 0)
    return tokens_in, tokens_out


def _messages_telemetry(state: dict[str, Any]) -> tuple[list[ToolCall], int, int]:
    """Default `telemetry_fn` for `_run_graph`: single mode's `create_react_agent`
    graph carries everything in one `state["messages"]` list.
    """
    tool_calls = _extract_tool_calls(state["messages"])
    tokens_in, tokens_out = _sum_usage(state["messages"])
    return tool_calls, tokens_in, tokens_out
