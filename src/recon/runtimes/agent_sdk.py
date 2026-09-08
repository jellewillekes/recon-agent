"""`Runtime` backed by the Claude Agent SDK, against the step-3 MCP server.

Spawns `recon.tools.mcp_server` as a stdio subprocess per run and drives
`claude_agent_sdk.query()` against it. See `prompts/investigator.md` for the
system prompt (never inline, per CLAUDE.md) and `config/models.yaml` for the
model, turn budget, and USD→EUR rate this reads.
"""

import asyncio
import contextlib
import json
import os
import re
import sys
import time
from collections.abc import AsyncGenerator
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

MCP_SERVER_NAME = "recon-tools"
_TOOL_NAMES = (
    "list_companies_tool",
    "list_financial_concepts_tool",
    "get_financial_fact_tool",
    "search_filings_tool",
    # Single mode has no worker/supervisor split - the one agent acts as
    # supervisor, so it gets the write path too (docs/contracts.md section 6:
    # "supervisor only").
    "flag_case_for_review_tool",
)
ALLOWED_TOOLS = [f"mcp__{MCP_SERVER_NAME}__{name}" for name in _TOOL_NAMES] + ["Read"]

# Passed to the MCP server subprocess's environment so flag_case_for_review_tool
# can populate ReviewFlag.created_by ("runtime + mode", docs/contracts.md section
# 6) without the agent having to self-report it as a tool argument.
_CREATED_BY = f"{RUNTIME_NAME}:single"

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


@dataclass
class RunBudget:
    """Per-run ceilings, read from `config/models.yaml`'s `run_budget:`
    section. "Per run" means the whole case, not one `query()` call — single
    mode makes one call so the distinction doesn't show, but multi mode's up
    to seven calls (decompose, up to four workers, synthesis, critic —
    `runtimes/multi_agent.py`) share one budget.
    """

    max_tool_calls: int
    max_tokens: int
    max_wall_clock_s: float


class _BudgetTracker:
    """Mutable, shared across every `_run_query` call in one run. Only
    tracks what can be checked mid-stream (tool calls, wall-clock) - token
    usage is only known once a call's `ResultMessage` arrives, so that
    budget is checked between calls instead (see `run_multi_async`).
    """

    def __init__(self, budget: RunBudget) -> None:
        self.budget = budget
        self._start = time.monotonic()
        self.tool_calls_used = 0

    def elapsed_s(self) -> float:
        return time.monotonic() - self._start

    def breach_reason(self) -> str | None:
        # >=, not >: this is checked right after tool_calls_used is
        # incremented for the call that just completed, so max_tool_calls is
        # the actual ceiling on calls that get to run, not one more than it.
        if self.tool_calls_used >= self.budget.max_tool_calls:
            return f"tool-call budget of {self.budget.max_tool_calls} exceeded"
        if self.elapsed_s() > self.budget.max_wall_clock_s:
            return f"wall-clock budget of {self.budget.max_wall_clock_s}s exceeded"
        return None


class _BudgetExceeded(Exception):
    """Raised by `_run_query` (tool-call/wall-clock) or by `run_multi_async`
    (token budget, checked between calls) on a mid-run breach. Carries
    everything gathered before the breach so the caller returns a partial
    `AgentResult` (`AgentSdkRuntime.run_async`'s `except _BudgetExceeded`)
    instead of crashing or silently discarding it.

    `answer`/`evidence`/`confidence` default to the empty/`"low"` values used
    when the breach lands before any answer exists (single mode; multi
    mode's decompose or worker calls). `run_multi_async` passes the real
    values through when a breach is detected after its supervisor-synthesis
    or critic call has already produced one — otherwise that already-valid
    answer would be thrown away along with the exception.
    """

    def __init__(
        self,
        reason: str,
        tool_calls: list[ToolCall],
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_eur: float = 0.0,
        answer: str = "",
        evidence: list[str] | None = None,
        confidence: _Confidence = "low",
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.tool_calls = tool_calls
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.cost_eur = cost_eur
        self.answer = answer
        self.evidence = evidence or []
        self.confidence = confidence


def _load_model_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        config: dict[str, Any] = yaml.safe_load(f)
    return config


def _load_run_budget(config: dict[str, Any]) -> RunBudget:
    section = config["run_budget"]
    return RunBudget(
        max_tool_calls=int(section["max_tool_calls"]),
        max_tokens=int(section["max_tokens"]),
        max_wall_clock_s=float(section["max_wall_clock_s"]),
    )


def _build_options(
    models_config_path: Path, prompt_path: Path
) -> tuple[ClaudeAgentOptions, float, RunBudget]:
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
                env={**os.environ, "RECON_CREATED_BY": _CREATED_BY},
            )
        },
        allowed_tools=ALLOWED_TOOLS,
        output_format=_ANSWER_SCHEMA,
    )
    return options, float(config["usd_to_eur_rate"]), _load_run_budget(config)


def _strip_tool_name(mcp_tool_name: str) -> str:
    prefix = f"mcp__{MCP_SERVER_NAME}__"
    name = mcp_tool_name.removeprefix(prefix)
    return name.removesuffix("_tool")


# When a tool result is too large to inline, the CLI replaces the
# `ToolResultBlock.content` with a notice like:
#   "Error: result (53,048 characters) exceeds maximum allowed tokens. Output
#   has been saved to /home/user/.claude/projects/.../tool-results/
#   mcp-recon-tools-list_companies_tool-<ts>.txt.\nFormat: ..."
# instead of our tool's own JSON. The file it names holds exactly that JSON,
# on the same local filesystem, so we can recover the real status ourselves.
_OFFLOAD_NOTICE_RE = re.compile(r"Output has been saved to (?P<path>\S+?)\.\n")


def _parse_offloaded_result(text: str) -> dict[str, Any] | None:
    """Follow the CLI's oversized-result offload notice back to the real payload.

    Returns `None` (never raises) unless the notice names a file we can safely
    read: this text arrives inside tool output, which step 3's `search_filings`
    will eventually source from untrusted documents, so a forged notice must
    not be able to make us open an arbitrary path. We only follow one that
    resolves under the CLI's own per-session project directory and whose
    filename matches our own MCP server's offload naming convention.
    """
    match = _OFFLOAD_NOTICE_RE.search(text)
    if match is None:
        return None
    path = Path(match.group("path"))
    trusted_root = Path.home() / ".claude" / "projects"
    try:
        resolved = path.resolve()
        resolved.relative_to(trusted_root.resolve())
    except (OSError, ValueError):
        return None
    if not resolved.name.startswith(
        f"mcp-{MCP_SERVER_NAME}-"
    ) or not resolved.name.endswith(".txt"):
        return None
    if not resolved.is_file():
        return None
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _parse_tool_result(content: str | list[dict[str, Any]] | None) -> tuple[str, int]:
    """Pull `status`/`elapsed_ms` back out of the JSON our own `ToolResult` produced.

    Recognizes two shapes: the JSON directly, or (when the result was too
    large to inline) the CLI's offload notice, in which case the real payload
    is recovered from disk via `_parse_offloaded_result`. Falls back to
    `("unknown", 0)` for anything else — genuinely undeterminable, which is a
    different claim than `ToolResult`'s own `"unavailable"` ("source down,
    timeout, circuit open" per `docs/contracts.md`).

    `elapsed_ms` is optional in the payload: `ToolResult` always has it, but
    `ReviewFlagResult` (a write, not a timed read) doesn't, and a missing
    timing shouldn't erase a real `status`.
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
        payload = _parse_offloaded_result(text)
        if payload is None:
            return "unknown", 0
    status, elapsed_ms = payload.get("status"), payload.get("elapsed_ms", 0)
    if not isinstance(status, str) or not isinstance(elapsed_ms, int):
        return "unknown", 0
    return status, elapsed_ms


@dataclass
class _QueryResult:
    """What a single `query()` call produced, before schema-specific
    validation of `structured` — shared by single mode (`_ANSWER_SCHEMA`) and
    every multi-agent role (`runtimes/multi_agent.py`), each of which
    validates `structured` against its own `output_format` schema.
    """

    structured: dict[str, Any]
    tool_calls: list[ToolCall]
    tokens_in: int
    tokens_out: int
    cost_eur: float


async def _run_query(
    prompt: str,
    options: ClaudeAgentOptions,
    usd_to_eur_rate: float,
    tracker: _BudgetTracker | None = None,
) -> _QueryResult:
    """Drive one `query()` call to completion. Tool-call extraction, the
    oversized-result recovery path, and cost conversion are the same
    regardless of which role or `output_format` schema `options` configures —
    only `structured`'s shape varies by caller.

    `tracker`, when given, is checked after every message up until the
    `ResultMessage` arrives; a tool-call or wall-clock breach raises
    `_BudgetExceeded` and explicitly closes the stream (`contextlib.aclosing`,
    not a bare `break` — that would leave the generator merely suspended, not
    closed; ADR-0006 established that `query()`'s generator is safe to close
    mid-flight). Once `result_message` is set there is already a complete,
    valid answer to return — the budget is no longer checked, so a breach
    that lands on the very message carrying that answer doesn't discard it.
    """
    tool_calls: list[ToolCall] = []
    pending: dict[str, tuple[str, dict[str, Any]]] = {}
    result_message: ResultMessage | None = None

    mcp_prefix = f"mcp__{MCP_SERVER_NAME}__"
    # query()'s declared return type is AsyncIterator, but it's actually
    # implemented as an async generator (it has .aclose()) - the cast makes
    # that concrete for aclosing(), which needs it structurally.
    raw_stream = cast(
        "AsyncGenerator[object, None]", query(prompt=prompt, options=options)
    )
    async with contextlib.aclosing(raw_stream) as stream:
        async for message in stream:
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    # Only our four MCP tools are `tool_calls` in the AgentResult
                    # sense. `Read` (kept for the CLI's own oversized-result
                    # recovery, see _build_options) isn't one of tools/server.py's
                    # tools and its result doesn't match ToolResult's shape —
                    # recording it here would mislabel a successful recovery read
                    # with the generic "unknown" fallback status.
                    if isinstance(block, ToolUseBlock) and block.name.startswith(
                        mcp_prefix
                    ):
                        pending[block.id] = (_strip_tool_name(block.name), block.input)
            elif isinstance(message, UserMessage) and isinstance(message.content, list):
                for block in message.content:
                    if (
                        isinstance(block, ToolResultBlock)
                        and block.tool_use_id in pending
                    ):
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
                        if tracker is not None:
                            tracker.tool_calls_used += 1
            elif isinstance(message, ResultMessage):
                result_message = message

            if tracker is not None and result_message is None:
                reason = tracker.breach_reason()
                if reason is not None:
                    raise _BudgetExceeded(reason, tool_calls=list(tool_calls))

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

    usage = result_message.usage or {}
    tokens_in = (
        int(usage.get("input_tokens", 0))
        + int(usage.get("cache_creation_input_tokens", 0))
        + int(usage.get("cache_read_input_tokens", 0))
    )
    tokens_out = int(usage.get("output_tokens", 0))
    cost_eur = (result_message.total_cost_usd or 0.0) * usd_to_eur_rate

    return _QueryResult(
        structured=structured,
        tool_calls=tool_calls,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_eur=cost_eur,
    )


def _validate_answer(structured: dict[str, Any]) -> tuple[str, list[str], _Confidence]:
    """`_ANSWER_SCHEMA`'s own validation, split out of `_run_query` so it's
    reusable wherever an `answer`/`evidence`/`confidence` schema is used
    (single mode here; multi mode's supervisor-synthesis call in
    `runtimes/multi_agent.py`).
    """
    answer, evidence, confidence = (
        structured["answer"],
        list(structured["evidence"]),
        structured["confidence"],
    )
    if confidence not in _CONFIDENCE_VALUES:
        raise RuntimeError(
            f"agent_sdk run returned an invalid confidence: {confidence!r}."
        )
    return answer, evidence, cast(_Confidence, confidence)


class AgentSdkRuntime:
    """`Runtime` implementation driving the Claude Agent SDK."""

    def __init__(
        self,
        *,
        mode: Literal["single", "multi"] = "single",
        models_config_path: Path = DEFAULT_MODELS_CONFIG_PATH,
        prompt_path: Path = DEFAULT_PROMPT_PATH,
        roles_config_path: Path | None = None,
        prompts_dir: Path | None = None,
    ) -> None:
        self._mode = mode
        self._models_config_path = models_config_path
        self._prompt_path = prompt_path
        # Only meaningful for mode="multi"; None means "use multi_agent's own
        # defaults" (applied in run(), which is the only place that needs
        # runtimes.multi_agent - imported there, not at module level, since
        # multi_agent imports the query/validation primitives defined below
        # and a top-level import here would be circular).
        self._roles_config_path = roles_config_path
        self._prompts_dir = prompts_dir

    def run(self, case: Case) -> AgentResult:
        """Answer `case`. Never raises — the SDK/subprocess/schema failure modes
        this wraps are numerous and case-specific; the `Runtime` contract requires
        every one of them to surface as `AgentResult.error` instead of propagating.

        Sync wrapper for callers with no event loop of their own (CLI, eval
        harness). A caller that can await directly — the API — should use
        `run_async` instead: this method's own `asyncio.run()` gives it a
        second, disconnected event loop that an outer `asyncio.wait_for`
        cannot reach in to cancel.
        """
        return asyncio.run(self.run_async(case))

    async def run_async(self, case: Case) -> AgentResult:
        """Same contract as `run`, but a real coroutine: cancelling the await
        (e.g. via `asyncio.wait_for`) propagates into `query()` and, through
        it, the SDK's own cancellation-safe subprocess teardown — instead of
        being stranded in a separate event loop the caller can't reach.
        """
        start = time.monotonic()
        try:
            token_budget_note: str | None = None
            if self._mode == "multi":
                from recon.runtimes import multi_agent

                outcome = await multi_agent.run_multi_async(
                    case,
                    roles_config_path=self._roles_config_path,
                    prompts_dir=self._prompts_dir,
                )
            else:
                options, usd_to_eur_rate, budget = _build_options(
                    self._models_config_path, self._prompt_path
                )
                tracker = _BudgetTracker(budget)
                result = await _run_query(
                    case.question, options, usd_to_eur_rate, tracker=tracker
                )
                answer, evidence, confidence = _validate_answer(result.structured)
                outcome = _Outcome(
                    answer=answer,
                    evidence=evidence,
                    confidence=confidence,
                    tool_calls=result.tool_calls,
                    tokens_in=result.tokens_in,
                    tokens_out=result.tokens_out,
                    cost_eur=result.cost_eur,
                )
                # Single mode makes exactly one query() call, so there's no
                # "next call" to skip the way multi mode's _accumulate can -
                # the call has already finished with a complete, valid
                # answer by the time this is checked. Reported, not
                # prevented: the answer is kept, not discarded, since the
                # breach can't be un-happened after the fact.
                total_tokens = outcome.tokens_in + outcome.tokens_out
                if total_tokens > budget.max_tokens:
                    token_budget_note = (
                        f"token budget of {budget.max_tokens} exceeded "
                        f"({total_tokens} used) - reported after the fact, since "
                        "single mode's one call had already completed"
                    )
            return AgentResult(
                case_id=case.case_id,
                answer=outcome.answer,
                evidence=outcome.evidence,
                confidence=outcome.confidence,
                tool_calls=outcome.tool_calls,
                runtime=RUNTIME_NAME,
                mode=self._mode,
                tokens_in=outcome.tokens_in,
                tokens_out=outcome.tokens_out,
                cost_eur=outcome.cost_eur,
                elapsed_ms=int((time.monotonic() - start) * 1000),
                error=token_budget_note,
            )
        except _BudgetExceeded as exc:
            return AgentResult(
                case_id=case.case_id,
                answer=exc.answer,
                evidence=exc.evidence,
                confidence=exc.confidence,
                tool_calls=exc.tool_calls,
                runtime=RUNTIME_NAME,
                mode=self._mode,
                tokens_in=exc.tokens_in,
                tokens_out=exc.tokens_out,
                cost_eur=exc.cost_eur,
                elapsed_ms=int((time.monotonic() - start) * 1000),
                error=exc.reason,
            )
        except Exception as exc:  # noqa: BLE001 — boundary: see docstring
            return AgentResult(
                case_id=case.case_id,
                answer="",
                evidence=[],
                confidence="low",
                tool_calls=[],
                runtime=RUNTIME_NAME,
                mode=self._mode,
                tokens_in=0,
                tokens_out=0,
                cost_eur=0.0,
                elapsed_ms=int((time.monotonic() - start) * 1000),
                error=str(exc),
            )
