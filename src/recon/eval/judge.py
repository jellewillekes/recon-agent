"""LLM judge: checks rubric assertions against a produced answer.

One Agent SDK call per case, covering every dimension's assertions at once,
to cap judge cost at one extra model call per case rather than one per
dimension. Static assertions come from `config/rubrics/*.yaml`
(`docs/contracts.md` §8); `answer_correctness` has none of its own and is
instead expanded from the case's own per-question rubric
(`Case.context["rubric"]`, see `docs/data-sources.md`) — the dataset already
supplies question-specific correctness/contradiction criteria, so there's
nothing dataset-agnostic to assert there.
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from recon.contracts import AgentResult, Case
from recon.eval.rubrics import Rubric

DEFAULT_MODELS_CONFIG_PATH = Path("config/models.yaml")

ANSWER_CORRECTNESS_DIMENSION = "answer_correctness"

_JUDGE_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "holds": {"type": "boolean"},
                    },
                    "required": ["id", "holds"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["results"],
        "additionalProperties": False,
    },
}

_SYSTEM_PROMPT = (
    "You are a strict grader. For each numbered statement, decide only "
    "whether it is objectively true given the question, answer, evidence, "
    "and tool-call trace provided. Do not use outside knowledge of real "
    "companies, filings, or market events — judge only what's in front of "
    "you."
)


@dataclass(frozen=True)
class _Item:
    """One assertion to check, expanded from either a static rubric or a
    case's own per-question criteria."""

    id: str
    text: str
    dimension: str
    # True: the assertion should hold to count as satisfied (every static
    # assertion, and a per-case "correctness" criterion). False: the
    # assertion should NOT hold to count as satisfied (a per-case
    # "contradiction" criterion).
    expect: bool
    score_if_true: float


@dataclass(frozen=True)
class JudgeResult:
    rubric_scores: dict[str, float]
    cost_eur: float


def _static_items(rubric: Rubric) -> list[_Item]:
    return [
        _Item(
            id=f"{rubric.dimension}:{assertion.id}",
            text=assertion.text,
            dimension=rubric.dimension,
            expect=True,
            score_if_true=assertion.score_if_true,
        )
        for assertion in rubric.assertions
    ]


def _per_case_items(case: Case) -> list[_Item]:
    criteria = case.context.get("rubric") or []
    items: list[_Item] = []
    for index, entry in enumerate(criteria):
        operator = entry.get("operator")
        text = entry.get("criteria")
        if operator not in ("correctness", "contradiction") or not text:
            continue
        items.append(
            _Item(
                id=f"{ANSWER_CORRECTNESS_DIMENSION}:case_{index}",
                text=text,
                dimension=ANSWER_CORRECTNESS_DIMENSION,
                expect=(operator == "correctness"),
                score_if_true=1.0,
            )
        )
    return items


def build_judge_items(case: Case, rubrics: dict[str, Rubric]) -> list[_Item]:
    """Every assertion to check for this case."""
    items: list[_Item] = []
    for dimension, rubric in rubrics.items():
        if dimension == ANSWER_CORRECTNESS_DIMENSION:
            continue  # expanded from the case's own rubric below instead
        items.extend(_static_items(rubric))
    items.extend(_per_case_items(case))
    return items


def _build_prompt(case: Case, agent_result: AgentResult, items: list[_Item]) -> str:
    tool_call_lines = [
        f"  - {call.tool}({call.arguments}) -> {call.status}"
        for call in agent_result.tool_calls
    ] or ["  (none)"]
    assertion_lines = [f"- id={item.id}: {item.text}" for item in items]
    return "\n".join(
        [
            f"Question: {case.question}",
            f"Answer given: {agent_result.answer}",
            f"Evidence cited: {agent_result.evidence!r}",
            "Tool calls made, in order:",
            *tool_call_lines,
            "",
            (
                "For each statement below, decide whether it holds true, based "
                "only on the answer, evidence, and tool calls above."
            ),
            "",
            *assertion_lines,
        ]
    )


def _load_judge_config(models_config_path: Path) -> tuple[dict[str, Any], float]:
    with models_config_path.open(encoding="utf-8") as f:
        config: dict[str, Any] = yaml.safe_load(f)
    return config["judge"], float(config["usd_to_eur_rate"])


def _build_options(judge_config: dict[str, Any]) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=judge_config["model"],
        max_turns=judge_config["max_turns"],
        system_prompt=_SYSTEM_PROMPT,
        tools=[],
        allowed_tools=[],
        output_format=_JUDGE_SCHEMA,
    )


async def _run_async(
    prompt: str, options: ClaudeAgentOptions
) -> tuple[dict[str, bool], float]:
    result_message: ResultMessage | None = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            result_message = message

    if result_message is None:
        raise RuntimeError("judge query stream ended without a ResultMessage.")
    if result_message.is_error:
        raise RuntimeError(
            f"judge run failed: subtype={result_message.subtype!r} "
            f"errors={result_message.errors!r}"
        )

    structured = result_message.structured_output
    if not isinstance(structured, dict) or "results" not in structured:
        raise TypeError(
            f"judge run produced no structured output (subtype={result_message.subtype!r})."
        )
    holds = {entry["id"]: bool(entry["holds"]) for entry in structured["results"]}
    return holds, result_message.total_cost_usd or 0.0


def _score_dimensions(items: list[_Item], holds: dict[str, bool]) -> dict[str, float]:
    by_dimension: dict[str, list[_Item]] = {}
    for item in items:
        by_dimension.setdefault(item.dimension, []).append(item)

    scores: dict[str, float] = {}
    for dimension, dimension_items in by_dimension.items():
        total_weight = sum(item.score_if_true for item in dimension_items)
        if total_weight <= 0:
            scores[dimension] = 1.0
            continue
        earned = sum(
            item.score_if_true
            for item in dimension_items
            if holds.get(item.id, False) == item.expect
        )
        scores[dimension] = earned / total_weight
    return scores


def judge_case(
    case: Case,
    agent_result: AgentResult,
    rubrics: dict[str, Rubric],
    *,
    models_config_path: Path = DEFAULT_MODELS_CONFIG_PATH,
) -> JudgeResult:
    """Score every rubric dimension for one case's answer.

    A dimension with nothing to check (e.g. `answer_correctness` when the
    case carries no per-question rubric) defaults to 1.0, vacuously — not
    scored as a miss, same treatment as the tool-path N/A case in
    `metrics.py`.
    """
    items = build_judge_items(case, rubrics)

    if items:
        judge_config, usd_to_eur_rate = _load_judge_config(models_config_path)
        options = _build_options(judge_config)
        prompt = _build_prompt(case, agent_result, items)
        holds, cost_usd = asyncio.run(_run_async(prompt, options))
        scores = _score_dimensions(items, holds)
        cost_eur = cost_usd * usd_to_eur_rate
    else:
        scores = {}
        cost_eur = 0.0

    for dimension in rubrics:
        scores.setdefault(dimension, 1.0)

    return JudgeResult(rubric_scores=scores, cost_eur=cost_eur)
