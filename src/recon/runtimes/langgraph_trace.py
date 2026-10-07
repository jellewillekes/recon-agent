"""Reading a LangGraph run's messages: the tool calls it made, the tokens it
used, and the rows its tools returned. Shared by single and multi mode
(`langgraph.py`, `langgraph_multi.py`).
"""

import json
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from recon.contracts import ToolCall

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


def _parse_tool_message_status(content: Any) -> tuple[str, int]:
    """Pull `status`/`elapsed_ms` back out of the JSON our own `ToolResult`/
    `ReviewFlagResult` produced. `ToolMessage.content` from a real `ToolNode`
    run is a list of `{"type": "text", "text": "<json>"}` blocks (confirmed
    live against this project's own MCP server) - falls back to `("unknown",
    0)` for anything else, same spirit as `agent_sdk._parse_tool_result`.
    """
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
        return "unknown", 0
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return "unknown", 0
    status = payload.get("status")
    elapsed_ms = payload.get("elapsed_ms", 0)
    if not isinstance(status, str) or not isinstance(elapsed_ms, int):
        return "unknown", 0
    return status, elapsed_ms


def _extract_tool_calls(messages: list[BaseMessage]) -> list[ToolCall]:
    """Walk the graph's final message list pairing each `AIMessage.tool_calls`
    entry with its matching `ToolMessage` by `tool_call_id`. Only tool names
    in `_TOOL_NAMES` count - `response_format`'s own internal structured-
    output tool call (a real message in this list too) is excluded by not
    matching that allowlist, the same "only our real tools count" principle
    as `agent_sdk._run_query`'s `mcp_prefix` filter.
    """
    pending: dict[str, tuple[str, dict[str, Any]]] = {}
    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                if call["name"] in _TOOL_NAMES and call["id"] is not None:
                    pending[call["id"]] = (call["name"], call["args"])
    tool_calls: list[ToolCall] = []
    for message in messages:
        if isinstance(message, ToolMessage) and message.tool_call_id in pending:
            name, args = pending[message.tool_call_id]
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
