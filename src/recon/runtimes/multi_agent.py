"""Multi-agent mode for `AgentSdkRuntime` (`--mode multi`): a supervisor that
decomposes and routes, two workers each restricted to their own tool subset,
and a critic that checks the synthesized answer against its evidence.

See `docs/adr/0007-multi-agent-orchestration.md` for why this orchestrates
four separate `query()` calls in Python rather than the SDK's built-in
`agents=` subagent delegation, and why tool restriction is enforced via each
role's own `ClaudeAgentOptions.allowed_tools`/`mcp_servers` rather than in
`tools/mcp_server.py`.
"""

import os
import sys
from pathlib import Path
from typing import Any

import yaml
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk.types import McpStdioServerConfig

from recon.contracts import Case, ToolCall
from recon.runtimes.agent_sdk import (
    _ANSWER_SCHEMA,
    DEFAULT_MODELS_CONFIG_PATH,
    ISOLATED_SESSION,
    MCP_SERVER_NAME,
    RUNTIME_NAME,
    _BudgetExceeded,
    _BudgetTracker,
    _Confidence,
    _load_model_config,
    _load_run_budget,
    _Outcome,
    _PartialRun,
    _QueryResult,
    _run_query,
)
from recon.runtimes.answer import Answer, validate_answer
from recon.runtimes.api_key import without_api_keys
from recon.runtimes.evidence import RowIndex, worker_report
from recon.runtimes.providers import local_provider, routes

DEFAULT_ROLES_CONFIG_PATH = Path("config/roles.yaml")
DEFAULT_PROMPTS_DIR = Path("prompts")

WORKER_NAMES = ("worker_lookup", "worker_facts")

# See agent_sdk._CREATED_BY - same purpose, multi mode's value. Set on every
# role's MCP subprocess that gets one attached, though only the supervisor's
# roles.yaml tool subset ever actually includes flag_case_for_review. Built
# from RUNTIME_NAME rather than hardcoded, so it can't drift from
# agent_sdk._CREATED_BY's single-mode equivalent if RUNTIME_NAME ever changes.
_CREATED_BY = f"{RUNTIME_NAME}:multi"

_DECOMPOSE_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "subtasks": {
                "type": "array",
                "minItems": 1,
                # Structural cap on paid worker calls per case - not just the
                # prompt's "as few subtasks as it genuinely needs" - since a
                # worker's own max_turns already bounds one call's cost, but
                # nothing bounded how many calls one decomposition could
                # produce. 4 covers a two-company comparison (lookup + fact
                # per company) with room to spare.
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "worker": {"type": "string", "enum": list(WORKER_NAMES)},
                        "instruction": {"type": "string"},
                    },
                    "required": ["worker", "instruction"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["subtasks"],
        "additionalProperties": False,
    },
}

_WORKER_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "findings": {"type": "string"},
            # Refs of the rows the findings rest on (ADR 0030).
            "evidence_refs": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["findings", "evidence_refs"],
        "additionalProperties": False,
    },
}

_CRITIC_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "accepted": {"type": "boolean"},
            "reason": {"type": "string"},
        },
        "required": ["accepted", "reason"],
        "additionalProperties": False,
    },
}


def _load_roles_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        config: dict[str, Any] = yaml.safe_load(f)
    return config


def _build_role_options(
    role: str,
    role_config: dict[str, Any],
    prompts_dir: Path,
    output_format: dict[str, Any],
) -> ClaudeAgentOptions:
    """Build one role's options for one call. `role_config["tools"]` (absent
    for a role that calls no tools) is the structural restriction: a role with no
    entry gets no `mcp_servers` attached at all, not just an empty
    `allowed_tools` - the tool is structurally absent from that role's
    client, not refused after being offered.
    """
    tool_names: list[str] = role_config.get("tools", [])
    mcp_servers: dict[str, Any] = {}
    allowed_tools: list[str] = []
    if tool_names:
        mcp_servers[MCP_SERVER_NAME] = McpStdioServerConfig(
            command=sys.executable,
            args=["-m", "recon.tools.mcp_server"],
            env={
                **without_api_keys(os.environ),
                "RECON_CREATED_BY": _CREATED_BY,
                # This server lives for the whole run, so loading the
                # search models as it starts pays off (#99).
                "RECON_WARM_SEARCH_MODELS": "1",
            },
        )
        allowed_tools = [f"mcp__{MCP_SERVER_NAME}__{name}_tool" for name in tool_names]
        allowed_tools.append("Read")

    return ClaudeAgentOptions(
        model=role_config["model"],
        max_turns=role_config["max_turns"],
        system_prompt={
            "type": "file",
            "path": str((prompts_dir / f"{role}.md").resolve()),
        },
        tools=["Read"] if tool_names else [],
        mcp_servers=mcp_servers,
        allowed_tools=allowed_tools,
        output_format=output_format,
        **ISOLATED_SESSION,
    )


def _validate_decomposition(structured: dict[str, Any]) -> list[tuple[str, str]]:
    subtasks: list[tuple[str, str]] = []
    for item in structured["subtasks"]:
        worker, instruction = item["worker"], item["instruction"]
        if worker not in WORKER_NAMES:
            raise RuntimeError(f"supervisor routed to an unknown worker: {worker!r}.")
        subtasks.append((worker, instruction))
    return subtasks


def _validate_worker(structured: dict[str, Any]) -> tuple[str, list[str]]:
    return structured["findings"], [str(ref) for ref in structured["evidence_refs"]]


def _validate_critic(structured: dict[str, Any]) -> tuple[bool, str]:
    return bool(structured["accepted"]), structured["reason"]


async def run_multi_async(
    case: Case,
    *,
    roles_config_path: Path | None = None,
    prompts_dir: Path | None = None,
    models_config_path: Path = DEFAULT_MODELS_CONFIG_PATH,
    routing: bool = False,
) -> _Outcome:
    """Supervisor decomposes -> workers run their routed subtasks with their
    own restricted tool subset -> supervisor synthesizes -> critic checks the
    synthesis against its evidence, forcing confidence to "low" on rejection.
    No retry loop on rejection - see ADR-0007.

    With `routing`, the decompose step runs on the local model in
    `config/models.yaml`'s `routing:` section instead (step 14, ADR 0029).
    """
    roles_config_path = roles_config_path or DEFAULT_ROLES_CONFIG_PATH
    prompts_dir = prompts_dir or DEFAULT_PROMPTS_DIR
    roles_config = _load_roles_config(roles_config_path)
    model_config = _load_model_config(models_config_path)
    usd_to_eur_rate = float(model_config["usd_to_eur_rate"])
    budget = _load_run_budget(model_config, "multi")
    # Shared across all (up to seven) calls below - a case run's tool-call
    # and wall-clock budget, not one call's (agent_sdk.RunBudget's docstring).
    tracker = _BudgetTracker(budget)

    tokens_in = 0
    tokens_out = 0
    cost_eur = 0.0
    tool_calls: list[ToolCall] = []

    def _accumulate(
        result: _QueryResult,
        answer: Answer | None = None,
        confidence: _Confidence = "low",
    ) -> None:
        """Adds `result`'s usage (including its `tool_calls` - every call can
        have some now that the supervisor's own `flag_case_for_review` counts
        as one, not just workers') to the running totals; raises
        `_BudgetExceeded` if that pushes the run over `budget.max_tokens`.

        `answer`/`confidence`, when passed, are already validated
        from `result` itself (the supervisor-synthesis or critic call) — a
        breach here means that step's own tokens tipped the budget, but its
        output is already a complete, valid answer and must survive the
        exception instead of being discarded with it.
        """
        nonlocal tokens_in, tokens_out, cost_eur
        tokens_in += result.tokens_in
        tokens_out += result.tokens_out
        cost_eur += result.cost_eur
        tool_calls.extend(result.tool_calls)
        if tokens_in + tokens_out > budget.max_tokens:
            raise _BudgetExceeded(
                f"token budget of {budget.max_tokens} exceeded",
                tool_calls=list(tool_calls),
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_eur=cost_eur,
                answer=answer.answer if answer else "",
                evidence=answer.evidence if answer else None,
                confidence=confidence,
                claims=answer.claims if answer else None,
                evidence_items=answer.evidence_items if answer else None,
            )

    async def _run(prompt: str, options: ClaudeAgentOptions) -> _QueryResult:
        """`_run_query`, with a run that ended early (a budget breach or a failed
        call, #110) enriched with everything this run has accumulated across
        *prior* calls before it propagates - `_run_query` itself only knows
        about the call it's in.
        """
        try:
            return await _run_query(prompt, options, usd_to_eur_rate, tracker=tracker)
        except _PartialRun as exc:
            raise type(exc)(
                exc.reason,
                tool_calls=tool_calls + exc.tool_calls,
                tokens_in=tokens_in + exc.tokens_in,
                tokens_out=tokens_out + exc.tokens_out,
                cost_eur=cost_eur + exc.cost_eur,
            ) from exc

    decompose_prompt = (
        f"Case ID: {case.case_id}\n\n"
        f"Decompose this question into subtasks for your workers: {case.question}"
    )
    if routing and routes(model_config, "decompose"):
        reply = await local_provider(model_config).complete(
            (prompts_dir / "supervisor.md").read_text(encoding="utf-8"),
            decompose_prompt,
            _DECOMPOSE_SCHEMA["schema"],
        )
        # The local model's tokens are free and outside Claude's token
        # budget, so they aren't counted with Claude's (ADR 0029).
        decompose_result = _QueryResult(
            structured=reply.structured,
            tool_calls=[],
            tokens_in=0,
            tokens_out=0,
            cost_eur=reply.cost_eur,
        )
    else:
        decompose_options = _build_role_options(
            "supervisor", roles_config["supervisor"], prompts_dir, _DECOMPOSE_SCHEMA
        )
        decompose_result = await _run(decompose_prompt, decompose_options)
    _accumulate(decompose_result)
    subtasks = _validate_decomposition(decompose_result.structured)

    findings: list[str] = []
    rows = RowIndex()
    for worker, instruction in subtasks:
        worker_options = _build_role_options(
            worker, roles_config[worker], prompts_dir, _WORKER_SCHEMA
        )
        worker_result = await _run(instruction, worker_options)
        _accumulate(worker_result)
        rows.merge(worker_result.rows)
        worker_findings, worker_refs = _validate_worker(worker_result.structured)
        findings.append(worker_report(worker, worker_findings, worker_refs, rows))

    synthesize_options = _build_role_options(
        "supervisor", roles_config["supervisor"], prompts_dir, _ANSWER_SCHEMA
    )
    synthesis_prompt = (
        f"Case ID: {case.case_id}\n\n"
        f"Original question: {case.question}\n\n"
        "Worker findings:\n" + "\n\n".join(findings) + "\n\n"
        "Synthesize a final answer from these findings only."
    )
    synthesis_result = await _run(synthesis_prompt, synthesize_options)
    # The supervisor has the flag tool, so its own rows count too.
    rows.merge(synthesis_result.rows)
    answer = validate_answer(synthesis_result.structured, rows)
    confidence = answer.confidence
    _accumulate(synthesis_result, answer, confidence)

    critic_options = _build_role_options(
        "critic", roles_config["critic"], prompts_dir, _CRITIC_SCHEMA
    )
    critic_prompt = (
        f"Question: {case.question}\n\nProposed answer: {answer.answer}\n\n"
        "Cited evidence:\n" + "\n".join(answer.evidence) + "\n\n"
        "Does the evidence support the answer?"
    )
    try:
        critic_result = await _run(critic_prompt, critic_options)
    except _PartialRun as exc:
        # The synthesis is complete; only its check failed (#120). Keep the
        # answer, at "low" because nothing checked it.
        raise type(exc)(
            exc.reason,
            tool_calls=exc.tool_calls,
            tokens_in=exc.tokens_in,
            tokens_out=exc.tokens_out,
            cost_eur=exc.cost_eur,
            answer=answer.answer,
            evidence=answer.evidence,
            confidence="low",
            claims=answer.claims,
            evidence_items=answer.evidence_items,
        ) from exc
    accepted, _reason = _validate_critic(critic_result.structured)
    if not accepted:
        confidence = "low"
    _accumulate(critic_result, answer, confidence)

    return _Outcome(
        answer=answer.answer,
        evidence=answer.evidence,
        confidence=confidence,
        claims=answer.claims,
        evidence_items=answer.evidence_items,
        tool_calls=tool_calls,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_eur=cost_eur,
    )
