"""Faithfulness: are the answer's claims from filing text backed by the
passages the agent actually retrieved? (step 13, #18)

`AgentResult` keeps each tool call's arguments but not its output
(docs/contracts.md §4). `search_knowledge` is deterministic for a fixed index,
so the harness replays the agent's own queries to get back the passages it
saw, instead of storing them on `ToolCall`. See
docs/adr/0026-faithfulness-by-replaying-searches.md.

The score goes into `CaseScore.rubric_scores` under `faithfulness`. That isn't
a rubric dimension, so it stays out of `answer_score` and the rubric version
doesn't change. Cases that didn't search, or made no claim from filing text,
get no score rather than a vacuous 1.0.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions

from recon.contracts import AgentResult, Case
from recon.eval.judge import load_judge_config, structured_query
from recon.runtimes.agent_sdk import ISOLATED_SESSION

DIMENSION = "faithfulness"
SEARCH_TOOL = "search_knowledge"
DEFAULT_PROMPT_PATH = Path("prompts/judge_faithfulness.md")
DEFAULT_TOP_K = 5

# (query, top_k) -> the passages the search returns, best first. Each has at
# least chunk_id, form, filed, section and text.
Passages = Callable[[str, int, str | None], list[dict[str, Any]]]

_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string"},
                        "supported": {"type": "boolean"},
                    },
                    "required": ["claim", "supported"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["claims"],
        "additionalProperties": False,
    },
}


@dataclass(frozen=True)
class FaithfulnessResult:
    """One case's score, None when there was nothing to score, plus the
    judge call's usage for cost and tracing. Zero usage when no call ran."""

    score: float | None
    cost_eur: float = 0.0
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    num_turns: int = 0


def searched_queries(
    agent_result: AgentResult,
) -> list[tuple[str, int, str | None]]:
    """(query, top_k, company_id) of every search that returned passages, in
    order. The company filter is replayed too (#104)."""
    queries: list[tuple[str, int, str | None]] = []
    for call in agent_result.tool_calls:
        if call.tool != SEARCH_TOOL or call.status != "ok":
            continue
        query = call.arguments.get("query")
        if isinstance(query, str) and query:
            top_k = call.arguments.get("top_k", DEFAULT_TOP_K)
            company = call.arguments.get("company_id")
            queries.append(
                (
                    query,
                    top_k if isinstance(top_k, int) else DEFAULT_TOP_K,
                    company if isinstance(company, str) and company else None,
                )
            )
    return queries


def replay(agent_result: AgentResult, passages: Passages) -> list[dict[str, Any]]:
    """The passages the agent's searches returned, each once, in first-seen order."""
    seen: dict[str, dict[str, Any]] = {}
    for query, top_k, company_id in searched_queries(agent_result):
        for passage in passages(query, top_k, company_id):
            seen.setdefault(passage["chunk_id"], passage)
    return list(seen.values())


def build_prompt(
    case: Case, agent_result: AgentResult, retrieved: list[dict[str, Any]]
) -> str:
    """The judge's user message: the answer, then every retrieved passage."""
    passage_blocks = [
        f"[{p['chunk_id']} | {p['form']} filed {p['filed']} | {p['section']}]\n{p['text']}"
        for p in retrieved
    ]
    return "\n".join(
        [
            f"Question: {case.question}",
            f"Answer given: {agent_result.answer}",
            f"Evidence cited: {agent_result.evidence!r}",
            "",
            "Passages the agent retrieved from filing text:",
            "",
            "\n\n".join(passage_blocks),
        ]
    )


def score(claims: list[dict[str, Any]]) -> float | None:
    """Share of claims marked supported. None when the answer made none."""
    if not claims:
        return None
    return sum(1 for claim in claims if claim["supported"]) / len(claims)


def judge_faithfulness(
    case: Case,
    agent_result: AgentResult,
    passages: Passages,
    *,
    models_config_path: Path,
    prompt_path: Path = DEFAULT_PROMPT_PATH,
) -> FaithfulnessResult:
    """Replay the case's searches and judge the answer against the passages."""
    retrieved = replay(agent_result, passages)
    if not retrieved:
        return FaithfulnessResult(score=None)
    judge_config, usd_to_eur_rate = load_judge_config(models_config_path)
    options = ClaudeAgentOptions(
        model=judge_config["model"],
        max_turns=judge_config["max_turns"],
        system_prompt=prompt_path.read_text(encoding="utf-8"),
        tools=[],
        allowed_tools=[],
        output_format=_SCHEMA,
        **ISOLATED_SESSION,
    )
    call = asyncio.run(
        structured_query(build_prompt(case, agent_result, retrieved), options, "claims")
    )
    return FaithfulnessResult(
        score=score(call.structured["claims"]),
        cost_eur=call.cost_usd * usd_to_eur_rate,
        model=judge_config["model"],
        tokens_in=call.tokens_in,
        tokens_out=call.tokens_out,
        num_turns=call.num_turns,
    )
