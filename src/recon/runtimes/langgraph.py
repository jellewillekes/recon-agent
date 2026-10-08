"""`Runtime` backed by LangGraph, against the same step-3 MCP server the SDK
runtime uses (`tools/mcp_server.py`, unchanged) via `langchain-mcp-adapters`.

See `docs/adr/0010-langgraph-runtime.md` for why this needs a real API key,
read from `RECON_ANTHROPIC_API_KEY` (ADR 0027) (a second, separately-billed cost source alongside the
Agent SDK subscription credit `runtimes/agent_sdk.py` uses exclusively), why
`mcp` is pinned below 2.0 project-wide, and the tool-restriction mechanism.

Single mode (this module) is `langgraph.prebuilt.create_react_agent` — "a
ReAct graph" in LangGraph's own vocabulary. Multi mode is a hand-built
`StateGraph` in `runtimes/langgraph_multi.py` — same split as `agent_sdk.py`/
`multi_agent.py` (ADR-0007), and for the same reason: this module is already
long with single mode's own streaming/budget mechanics, which multi mode
reuses (`_run_graph`, `_build_react_subgraph`, `_build_checkpointer`) rather
than duplicating.
"""

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any, Literal

import yaml
from langchain_anthropic import ChatAnthropic
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StdioConnection
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel

from recon.contracts import AgentResult, Case, ToolCall
from recon.runtimes.answer import validate_answer
from recon.runtimes.api_key import langgraph_api_key, without_api_keys
from recon.runtimes.evidence import RowIndex
from recon.runtimes.langgraph_run import (
    _budget_breach_note,
    _BudgetExceeded,
    _build_checkpointer,
    _compute_cost_eur,
    _failed_outcome,
    _Outcome,
    _Paused,
    _run_graph,
)
from recon.runtimes.langgraph_trace import (
    _extract_tool_calls,
    _sum_usage,
    row_index,
    tool_row_records,
)
from recon.runtimes.run_budget import budget_section

RUNTIME_NAME = "langgraph"

MCP_SERVER_NAME = "recon-tools"

# Passed to the MCP server subprocess's environment, same convention as
# agent_sdk._CREATED_BY - without it, mcp_server.py's _call_flag_case_for_review
# falls back to the wrong "agent_sdk:unknown" label for every flag made
# through this runtime.
_CREATED_BY = f"{RUNTIME_NAME}:single"

DEFAULT_MODELS_CONFIG_PATH = Path("config/models.yaml")
DEFAULT_PROMPT_PATH = Path("prompts/investigator.md")


class FigureResponse(BaseModel):
    """A claim's figure as the model gives it, the shape of
    `answer.FIGURE_SCHEMA` (#148). `value` isn't checked here, so a malformed
    one is dropped by `resolve_claims` instead of failing the answer."""

    kind: Literal["level", "growth", "ratio"]
    value: str
    scale: Literal["units", "thousands", "millions", "billions", "percent"]


class ClaimResponse(BaseModel):
    """One claim in the answer and the refs of the rows it rests on, the
    shape of `answer.CLAIMS_SCHEMA` (ADR 0030)."""

    text: str
    importance: Literal["key", "supporting"]
    evidence_refs: list[str]
    figure: FigureResponse | None = None


class AnswerResponse(BaseModel):
    """The answer as claims citing row refs, like the Agent SDK runtime's
    (#118, ADR 0030), passed as `create_react_agent`'s `response_format`. The
    refs are resolved against the rows the run's tools returned.

    `flag_reason` is multi mode's own extension (issue #14 part 2): the
    supervisor's synthesize call uses this same schema (`langgraph_multi.py`),
    and sets this field instead of calling a bound tool - see
    `docs/adr/0010-langgraph-runtime.md` for why. Always `None` in single
    mode, which has no dedicated confirm-flag node to act on it.
    """

    answer: str
    claims: list[ClaimResponse]
    confidence: Literal["high", "medium", "low"]
    flag_reason: str | None = None


def resolved_outcome(
    response: AnswerResponse,
    rows: RowIndex,
    *,
    tool_calls: list[ToolCall],
    tokens_in: int,
    tokens_out: int,
    cost_eur: float,
) -> _Outcome:
    """An `_Outcome` with `response`'s claims resolved against `rows`, the
    same resolution the Agent SDK runtime uses (`answer.validate_answer`)."""
    answer = validate_answer(response.model_dump(), rows)
    return _Outcome(
        answer=answer.answer,
        evidence=answer.evidence,
        confidence=answer.confidence,
        tool_calls=tool_calls,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_eur=cost_eur,
        claims=answer.claims,
        evidence_items=answer.evidence_items,
    )


def _load_model_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        config: dict[str, Any] = yaml.safe_load(f)
    return config


def _mcp_connection(env: dict[str, str] | None = None) -> StdioConnection:
    """One `langchain_mcp_adapters` stdio connection entry, spawning the same
    `-m recon.tools.mcp_server` subprocess the SDK runtime uses.
    """
    return StdioConnection(
        transport="stdio",
        command=sys.executable,
        args=["-m", "recon.tools.mcp_server"],
        env=env,
    )


async def _build_react_subgraph(
    *,
    model_name: str,
    prompt: str,
    response_format: type[BaseModel],
    created_by: str,
    tool_names: tuple[str, ...] | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
) -> Any:
    """Build one `create_react_agent` graph against the project's MCP server:
    spawn the subprocess, fetch its tools, optionally filter them down to
    `tool_names` (ADR-0010's Python-side tool restriction - `None` means
    every tool, single mode's own subset), bind `prompt`/`response_format`.

    Reused for single mode's one investigator (this module, `tool_names=None`,
    a real `checkpointer` since its whole run goes through `_run_graph`'s
    thread-scoped `astream`) and for each multi-mode worker
    (`langgraph_multi.py`, `tool_names` from `config/roles.yaml`,
    `checkpointer=None` - workers are invoked directly with `.ainvoke()`,
    with no thread/interrupt needs of their own).
    """
    env = {**without_api_keys(os.environ), "RECON_CREATED_BY": created_by}
    # No explicit teardown here - verified directly against this project's
    # installed langchain-mcp-adapters source, not just its docs (round 3
    # review of PR #46 asked for this): both get_tools()'s discovery call and
    # every individual bound tool's execution (convert_mcp_tool_to_langchain_tool's
    # call_tool) scope their own subprocess session inside `async with
    # create_session(...)`, torn down via the context manager protocol on
    # success, exception, or cancellation alike - MultiServerMCPClient itself
    # never holds a persistent session to close, unlike agent_sdk.py's one
    # long-lived query() stream (contextlib.aclosing in _run_query).
    client = MultiServerMCPClient({MCP_SERVER_NAME: _mcp_connection(env=env)})
    tools = await client.get_tools()
    if tool_names is not None:
        wanted = {f"{name}_tool" for name in tool_names}
        tools = [tool for tool in tools if tool.name in wanted]

    # mypy's stub for ChatAnthropic's generated __init__ doesn't surface
    # `model` as a valid kwarg, though it's a genuine pydantic field
    # (confirmed: ChatAnthropic.model_fields, and constructs fine at
    # runtime) - a stub gap, not a real type error.
    model = ChatAnthropic(model=model_name, api_key=langgraph_api_key())  # type: ignore[call-arg]
    return create_react_agent(
        model,
        tools,
        prompt=prompt,
        response_format=response_format,
        checkpointer=checkpointer,
    )


class LangGraphRuntime:
    """`Runtime` implementation driving LangGraph. Single mode (this module's
    own `create_react_agent` graph) and multi mode (`runtimes/
    langgraph_multi.py`'s hand-built `StateGraph`, ADR-0010) share this one
    entry point.
    """

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
        # Only meaningful for mode="multi" - None means "use langgraph_multi's
        # own defaults" (mirrors AgentSdkRuntime.__init__'s identical
        # roles_config_path/prompts_dir pattern; not imported at module level
        # for the same reason agent_sdk.py doesn't import multi_agent at
        # module level - see run_async's lazy import below).
        self._roles_config_path = roles_config_path
        self._prompts_dir = prompts_dir
        # Built lazily and reused across every run_async/resume call this
        # instance makes (_get_checkpointer) - required for InMemorySaver
        # (its storage lives only in this one Python object; a fresh one per
        # call would make every interrupt unresumable) and kept for
        # AsyncPostgresSaver too rather than reconnecting per call.
        self._checkpointer: BaseCheckpointSaver | None = None

    async def _get_checkpointer(self) -> BaseCheckpointSaver:
        if self._checkpointer is None:
            self._checkpointer = await _build_checkpointer(
                os.environ.get("DATABASE_URL")
            )
        return self._checkpointer

    def _agent_result(
        self, case: Case, start: float, outcome: _Outcome, error: str | None
    ) -> AgentResult:
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
            error=error,
            claims=outcome.claims,
            evidence_items=outcome.evidence_items,
        )

    def run(self, case: Case) -> AgentResult:
        """Answer `case`. Never raises - see `AgentSdkRuntime.run`'s docstring;
        the `Runtime` contract requires every failure to surface as
        `AgentResult.error` instead.
        """
        return asyncio.run(self.run_async(case))

    async def run_async(self, case: Case) -> AgentResult:
        start = time.monotonic()
        model_config: dict[str, Any] = {}
        model_name = ""
        try:
            model_config = _load_model_config(self._models_config_path)
            run_budget = budget_section(model_config, self._mode)
            max_tool_calls = int(run_budget["max_tool_calls"])
            max_tokens = int(run_budget["max_tokens"])
            max_wall_clock_s = float(run_budget["max_wall_clock_s"])

            if self._mode == "multi":
                from recon.runtimes import langgraph_multi

                outcome = await langgraph_multi.run_multi_async(
                    case,
                    checkpointer=await self._get_checkpointer(),
                    model_config=model_config,
                    max_tool_calls=max_tool_calls,
                    max_wall_clock_s=max_wall_clock_s,
                    max_turns=int(model_config["investigator"]["max_turns"]),
                    roles_config_path=self._roles_config_path,
                    prompts_dir=self._prompts_dir,
                )
            else:
                investigator = model_config["investigator"]
                model_name = investigator["model"]
                prompt = self._prompt_path.read_text(encoding="utf-8")
                graph = await _build_react_subgraph(
                    model_name=model_name,
                    prompt=prompt,
                    response_format=AnswerResponse,
                    created_by=_CREATED_BY,
                    checkpointer=await self._get_checkpointer(),
                )
                result = await _run_graph(
                    graph,
                    case,
                    investigator["max_turns"],
                    max_wall_clock_s,
                    max_tool_calls,
                )

                structured = result["structured_response"]
                if not isinstance(structured, AnswerResponse):
                    raise TypeError(
                        "langgraph run produced no structured answer "
                        f"(got {type(structured)!r})."
                    )
                messages = result["messages"]
                tokens_in, tokens_out = _sum_usage(messages)
                outcome = resolved_outcome(
                    structured,
                    row_index(tool_row_records(messages)),
                    tool_calls=_extract_tool_calls(messages),
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_eur=_compute_cost_eur(
                        model_config, model_name, tokens_in, tokens_out
                    ),
                )

            note = _budget_breach_note(
                outcome.tokens_in, outcome.tokens_out, max_tokens
            )
            return self._agent_result(case, start, outcome, note)
        except _Paused as exc:
            # Not a failure - decompose, workers, synthesize, and critic
            # already produced a real, complete answer; only the review-flag
            # write is pending a human decision. See _Paused's docstring.
            return self._agent_result(case, start, exc.outcome, str(exc))
        except _BudgetExceeded as exc:
            # Single mode computes cost here, from its one well-defined
            # model_name (unchanged from part 1) - multi mode has no single
            # model_name to use here, so langgraph_multi.py computes it
            # itself and carries it on the exception (see _BudgetExceeded's
            # docstring).
            cost_eur = (
                exc.cost_eur
                if self._mode == "multi"
                else _compute_cost_eur(
                    model_config, model_name, exc.tokens_in, exc.tokens_out
                )
            )
            spent = _failed_outcome(
                exc.tool_calls, exc.tokens_in, exc.tokens_out, cost_eur
            )
            return self._agent_result(case, start, spent, exc.reason)
        except Exception as exc:  # noqa: BLE001 — boundary: see run's docstring
            return self._agent_result(case, start, _failed_outcome(), str(exc))

    async def resume(self, case: Case, thread_id: str, approved: bool) -> AgentResult:
        """Complete a multi-mode run paused at `_confirm_flag_node`
        (`run_async`'s `AgentResult.error` names the `thread_id` to pass
        here). Outside the `Runtime` protocol - issue #14 part 2 asks for the
        interrupt mechanism only, no CLI wiring yet; a natural, separate
        follow-up. Tests call this directly.

        Only meaningful for `mode="multi"` - single mode never pauses, so
        there is never a `thread_id` to resume.
        """
        start = time.monotonic()
        model_config: dict[str, Any] = {}
        try:
            if self._mode != "multi":
                raise RuntimeError(
                    "LangGraphRuntime.resume is only meaningful for "
                    "mode='multi' - single mode never pauses."
                )
            model_config = _load_model_config(self._models_config_path)
            run_budget = budget_section(model_config, self._mode)
            max_tool_calls = int(run_budget["max_tool_calls"])
            max_tokens = int(run_budget["max_tokens"])
            max_wall_clock_s = float(run_budget["max_wall_clock_s"])

            from recon.runtimes import langgraph_multi

            outcome = await langgraph_multi.resume_multi_async(
                case,
                thread_id,
                approved,
                checkpointer=await self._get_checkpointer(),
                model_config=model_config,
                max_tool_calls=max_tool_calls,
                max_wall_clock_s=max_wall_clock_s,
                max_turns=int(model_config["investigator"]["max_turns"]),
                roles_config_path=self._roles_config_path,
                prompts_dir=self._prompts_dir,
            )
            note = _budget_breach_note(
                outcome.tokens_in, outcome.tokens_out, max_tokens
            )
            return self._agent_result(case, start, outcome, note)
        except _BudgetExceeded as exc:
            spent = _failed_outcome(
                exc.tool_calls, exc.tokens_in, exc.tokens_out, exc.cost_eur
            )
            return self._agent_result(case, start, spent, exc.reason)
        except Exception as exc:  # noqa: BLE001 — boundary: see run's docstring
            return self._agent_result(case, start, _failed_outcome(), str(exc))
