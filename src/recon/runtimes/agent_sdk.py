"""`Runtime` backed by the Claude Agent SDK, against the step-3 MCP server.

Spawns `recon.tools.mcp_server` as a stdio subprocess per run and drives
`claude_agent_sdk.query()` against it. See `prompts/investigator.md` for the
system prompt (never inline, per CLAUDE.md) and `config/models.yaml` for the
model, turn budget, and USD→EUR rate this reads.
"""

import asyncio
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast, get_args

import yaml
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)
from claude_agent_sdk.types import McpStdioServerConfig

from recon.contracts import AgentResult, Case, ToolCall

RUNTIME_NAME = "agent_sdk"
MODE: Literal["single", "multi"] = "single"

MCP_SERVER_NAME = "recon-tools"
_TOOL_NAMES = (
    "list_companies_tool",
    "list_financial_concepts_tool",
    "get_financial_fact_tool",
    "search_filings_tool",
)
ALLOWED_TOOLS = [f"mcp__{MCP_SERVER_NAME}__{name}" for name in _TOOL_NAMES] + ["Read"]

DEFAULT_MODELS_CONFIG_PATH = Path("config/models.yaml")
DEFAULT_PROMPT_PATH = Path("prompts/investigator.md")

_ANSWER_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        },
        "required": ["answer", "evidence", "confidence"],
        "additionalProperties": False,
    },
}


_Confidence = Literal["high", "medium", "low"]
_CONFIDENCE_VALUES = set(get_args(_Confidence))


@dataclass
class _Outcome:
    """What a completed (non-erroring) run produced, before `elapsed_ms` is known."""

    answer: str
    evidence: list[str]
    confidence: _Confidence
    tool_calls: list[ToolCall]
    tokens_in: int
    tokens_out: int
    cost_eur: float


def _load_model_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        config: dict[str, Any] = yaml.safe_load(f)
    return config


def _build_options(
    models_config_path: Path, prompt_path: Path
) -> tuple[ClaudeAgentOptions, float]:
    config = _load_model_config(models_config_path)
    investigator = config["investigator"]
    options = ClaudeAgentOptions(
        model=investigator["model"],
        # Every run spends real Agent SDK credit (CLAUDE.md's Cost section) —
        # a case that confuses the model into a retry loop must still stop.
        max_turns=investigator["max_turns"],
        system_prompt={"type": "file", "path": str(prompt_path.resolve())},
        # Read is the one built-in tool kept: when an MCP tool result is too
        # large to hand back inline, the CLI offloads it to a file and tells
        # the model to Read it back — without this, that recovery path is a
        # dead end and the run stalls out retrying. Bash/Write/Edit/... stay
        # off; the only other capability is the MCP tool subset below.
        tools=["Read"],
        mcp_servers={
            MCP_SERVER_NAME: McpStdioServerConfig(
                command=sys.executable,
                args=["-m", "recon.tools.mcp_server"],
            )
        },
        allowed_tools=ALLOWED_TOOLS,
        output_format=_ANSWER_SCHEMA,
    )
    return options, float(config["usd_to_eur_rate"])


def _strip_tool_name(mcp_tool_name: str) -> str:
    prefix = f"mcp__{MCP_SERVER_NAME}__"
    name = mcp_tool_name.removeprefix(prefix)
    return name.removesuffix("_tool")


def _parse_tool_result(content: str | list[dict[str, Any]] | None) -> tuple[str, int]:
    """Pull `status`/`elapsed_ms` back out of the JSON our own `ToolResult` produced.

    Falls back to `("unavailable", 0)` for anything that doesn't parse as the
    shape `tools/server.py` returns — the tool ran, but we can't account for it.
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
        return "unavailable", 0
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return "unavailable", 0
    status, elapsed_ms = payload.get("status"), payload.get("elapsed_ms")
    if not isinstance(status, str) or not isinstance(elapsed_ms, int):
        return "unavailable", 0
    return status, elapsed_ms


async def _run_async(
    case: Case, options: ClaudeAgentOptions, usd_to_eur_rate: float
) -> _Outcome:
    tool_calls: list[ToolCall] = []
    pending: dict[str, tuple[str, dict[str, Any]]] = {}
    result_message: ResultMessage | None = None

    mcp_prefix = f"mcp__{MCP_SERVER_NAME}__"
    async for message in query(prompt=case.question, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                # Only our four MCP tools are `tool_calls` in the AgentResult
                # sense. `Read` (kept for the CLI's own oversized-result
                # recovery, see _build_options) isn't one of tools/server.py's
                # tools and its result doesn't match ToolResult's shape —
                # recording it here would mislabel a successful recovery read
                # as ToolResult status "unavailable" (contracts.md section 3:
                # "source down, timeout, circuit open").
                if isinstance(block, ToolUseBlock) and block.name.startswith(
                    mcp_prefix
                ):
                    pending[block.id] = (_strip_tool_name(block.name), block.input)
        elif isinstance(message, UserMessage) and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, ToolResultBlock) and block.tool_use_id in pending:
                    tool_name, arguments = pending.pop(block.tool_use_id)
                    status, elapsed_ms = _parse_tool_result(block.content)
                    tool_calls.append(
                        ToolCall(
                            tool=tool_name,
                            arguments=arguments,
                            status=status,
                            elapsed_ms=elapsed_ms,
                        )
                    )
        elif isinstance(message, ResultMessage):
            result_message = message

    if result_message is None:
        raise RuntimeError("agent_sdk query stream ended without a ResultMessage.")
    if result_message.is_error:
        raise RuntimeError(
            f"agent_sdk run failed: subtype={result_message.subtype!r} "
            f"errors={result_message.errors!r}"
        )

    structured = result_message.structured_output
    if not isinstance(structured, dict):
        raise TypeError(
            f"agent_sdk run produced no structured answer (subtype={result_message.subtype!r})."
        )
    answer, evidence, confidence = (
        structured["answer"],
        list(structured["evidence"]),
        structured["confidence"],
    )
    if confidence not in _CONFIDENCE_VALUES:
        raise RuntimeError(
            f"agent_sdk run returned an invalid confidence: {confidence!r}."
        )
    confidence = cast(_Confidence, confidence)

    usage = result_message.usage or {}
    tokens_in = (
        int(usage.get("input_tokens", 0))
        + int(usage.get("cache_creation_input_tokens", 0))
        + int(usage.get("cache_read_input_tokens", 0))
    )
    tokens_out = int(usage.get("output_tokens", 0))
    cost_eur = (result_message.total_cost_usd or 0.0) * usd_to_eur_rate

    return _Outcome(
        answer=answer,
        evidence=evidence,
        confidence=confidence,
        tool_calls=tool_calls,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_eur=cost_eur,
    )


class AgentSdkRuntime:
    """`Runtime` implementation driving the Claude Agent SDK."""

    def __init__(
        self,
        *,
        models_config_path: Path = DEFAULT_MODELS_CONFIG_PATH,
        prompt_path: Path = DEFAULT_PROMPT_PATH,
    ) -> None:
        self._models_config_path = models_config_path
        self._prompt_path = prompt_path

    def run(self, case: Case) -> AgentResult:
        """Answer `case`. Never raises — the SDK/subprocess/schema failure modes
        this wraps are numerous and case-specific; the `Runtime` contract requires
        every one of them to surface as `AgentResult.error` instead of propagating.
        """
        start = time.monotonic()
        try:
            options, usd_to_eur_rate = _build_options(
                self._models_config_path, self._prompt_path
            )
            outcome = asyncio.run(_run_async(case, options, usd_to_eur_rate))
            return AgentResult(
                case_id=case.case_id,
                answer=outcome.answer,
                evidence=outcome.evidence,
                confidence=outcome.confidence,
                tool_calls=outcome.tool_calls,
                runtime=RUNTIME_NAME,
                mode=MODE,
                tokens_in=outcome.tokens_in,
                tokens_out=outcome.tokens_out,
                cost_eur=outcome.cost_eur,
                elapsed_ms=int((time.monotonic() - start) * 1000),
                error=None,
            )
        except Exception as exc:  # noqa: BLE001 — boundary: see docstring
            return AgentResult(
                case_id=case.case_id,
                answer="",
                evidence=[],
                confidence="low",
                tool_calls=[],
                runtime=RUNTIME_NAME,
                mode=MODE,
                tokens_in=0,
                tokens_out=0,
                cost_eur=0.0,
                elapsed_ms=int((time.monotonic() - start) * 1000),
                error=str(exc),
            )
