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
    MCP_SERVER_NAME,
    _load_model_config,
    _Outcome,
    _QueryResult,
    _run_query,
    _validate_answer,
)

DEFAULT_ROLES_CONFIG_PATH = Path("config/roles.yaml")
DEFAULT_PROMPTS_DIR = Path("prompts")

WORKER_NAMES = ("worker_lookup", "worker_facts")

# See agent_sdk._CREATED_BY - same purpose, multi mode's value. Set on every
# role's MCP subprocess that gets one attached, though only the supervisor's
# roles.yaml tool subset ever actually includes flag_case_for_review.
_CREATED_BY = "agent_sdk:multi"

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
            "evidence": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["findings", "evidence"],
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
    for supervisor/critic) is the structural restriction: a role with no
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
            env={**os.environ, "RECON_CREATED_BY": _CREATED_BY},
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
    return structured["findings"], list(structured["evidence"])


def _validate_critic(structured: dict[str, Any]) -> tuple[bool, str]:
    return bool(structured["accepted"]), structured["reason"]


async def run_multi_async(
    case: Case,
    *,
    roles_config_path: Path | None = None,
    prompts_dir: Path | None = None,
    models_config_path: Path = DEFAULT_MODELS_CONFIG_PATH,
) -> _Outcome:
    """Supervisor decomposes -> workers run their routed subtasks with their
    own restricted tool subset -> supervisor synthesizes -> critic checks the
    synthesis against its evidence, forcing confidence to "low" on rejection.
    No retry loop on rejection - see ADR-0007.
    """
    roles_config_path = roles_config_path or DEFAULT_ROLES_CONFIG_PATH
    prompts_dir = prompts_dir or DEFAULT_PROMPTS_DIR
    roles_config = _load_roles_config(roles_config_path)
    usd_to_eur_rate = float(_load_model_config(models_config_path)["usd_to_eur_rate"])

    tokens_in = 0
    tokens_out = 0
    cost_eur = 0.0
    tool_calls: list[ToolCall] = []

    def _accumulate(result: _QueryResult) -> None:
        nonlocal tokens_in, tokens_out, cost_eur
        tokens_in += result.tokens_in
        tokens_out += result.tokens_out
        cost_eur += result.cost_eur

    decompose_options = _build_role_options(
        "supervisor", roles_config["supervisor"], prompts_dir, _DECOMPOSE_SCHEMA
    )
    decompose_result = await _run_query(
        f"Decompose this question into subtasks for your workers: {case.question}",
        decompose_options,
        usd_to_eur_rate,
    )
    _accumulate(decompose_result)
    subtasks = _validate_decomposition(decompose_result.structured)

    findings: list[str] = []
    for worker, instruction in subtasks:
        worker_options = _build_role_options(
            worker, roles_config[worker], prompts_dir, _WORKER_SCHEMA
        )
        worker_result = await _run_query(instruction, worker_options, usd_to_eur_rate)
        _accumulate(worker_result)
        worker_findings, worker_evidence = _validate_worker(worker_result.structured)
        tool_calls.extend(worker_result.tool_calls)
        findings.append(
            f"[{worker}] findings: {worker_findings}\nevidence: {worker_evidence}"
        )

    synthesize_options = _build_role_options(
        "supervisor", roles_config["supervisor"], prompts_dir, _ANSWER_SCHEMA
    )
    synthesis_prompt = (
        f"Original question: {case.question}\n\n"
        "Worker findings:\n" + "\n\n".join(findings) + "\n\n"
        "Synthesize a final answer from these findings only."
    )
    synthesis_result = await _run_query(
        synthesis_prompt, synthesize_options, usd_to_eur_rate
    )
    _accumulate(synthesis_result)
    answer, evidence, confidence = _validate_answer(synthesis_result.structured)

    critic_options = _build_role_options(
        "critic", roles_config["critic"], prompts_dir, _CRITIC_SCHEMA
    )
    critic_prompt = (
        f"Question: {case.question}\n\nProposed answer: {answer}\n\n"
        f"Cited evidence: {evidence}\n\nDoes the evidence support the answer?"
    )
    critic_result = await _run_query(critic_prompt, critic_options, usd_to_eur_rate)
    _accumulate(critic_result)
    accepted, _reason = _validate_critic(critic_result.structured)
    if not accepted:
        confidence = "low"

    return _Outcome(
        answer=answer,
        evidence=evidence,
        confidence=confidence,
        tool_calls=tool_calls,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_eur=cost_eur,
    )
