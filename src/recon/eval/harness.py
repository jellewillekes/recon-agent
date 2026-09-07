"""Orchestrates a full evaluation run: score every case, hash prompts and
model config, assemble an `EvalRun`. See `docs/contracts.md` §7.
"""

from datetime import UTC, datetime
from pathlib import Path

from recon.adapters.finance_agent_bench import ATTRIBUTION, LICENSE, SOURCE
from recon.contracts import AgentResult, Case, CaseScore, EvalRun
from recon.eval import hashing, metrics
from recon.eval.judge import DEFAULT_MODELS_CONFIG_PATH, judge_case
from recon.eval.rubrics import DEFAULT_RUBRICS_DIR, Rubric, load_rubrics
from recon.runtimes.base import Runtime

# Bump when config/rubrics/*.yaml assertions change (docs/contracts.md §8:
# "Rubric changes are breaking: earlier runs are no longer comparable.").
RUBRIC_VERSION = "1"


def _run_id() -> str:
    return f"eval-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"


def score_case(
    case: Case,
    runtime: Runtime,
    rubrics: dict[str, Rubric],
    *,
    models_config_path: Path = DEFAULT_MODELS_CONFIG_PATH,
) -> tuple[CaseScore, AgentResult]:
    agent_result = runtime.run(case)

    exact, exact_note = metrics.tool_path_exact(case, agent_result)
    equivalent, _ = metrics.tool_path_equivalent(case, agent_result)

    notes_parts: list[str] = []
    if agent_result.error is not None:
        notes_parts.append(f"runtime error: {agent_result.error}")
    if exact_note is not None:
        notes_parts.append(exact_note)

    if agent_result.error is not None:
        # A failed run has nothing meaningful to grade — score it as a hard
        # miss rather than spending a judge call on an empty/garbage answer.
        rubric_scores = {dimension: 0.0 for dimension in rubrics}
        answer_score = 0.0
        judge_cost_eur = 0.0
    else:
        judge_result = judge_case(
            case, agent_result, rubrics, models_config_path=models_config_path
        )
        rubric_scores = judge_result.rubric_scores
        answer_score = rubric_scores.get("answer_correctness", 0.0)
        judge_cost_eur = judge_result.cost_eur

    score = CaseScore(
        case_id=case.case_id,
        task_completion=metrics.task_completion(agent_result),
        answer_score=answer_score,
        tool_path_exact=exact,
        tool_path_equivalent=equivalent,
        tool_call_accuracy=metrics.tool_call_accuracy(agent_result),
        rubric_scores=rubric_scores,
        cost_eur=agent_result.cost_eur + judge_cost_eur,
        elapsed_ms=agent_result.elapsed_ms,
        notes="; ".join(notes_parts),
    )
    return score, agent_result


def _aggregate(cases: list[Case], case_scores: list[CaseScore]) -> dict[str, float]:
    n = len(case_scores)
    if n == 0:
        return {}

    applicable = [
        score
        for case, score in zip(cases, case_scores, strict=True)
        if case.expected_tool_path is not None
    ]

    aggregate: dict[str, float] = {
        "case_count": float(n),
        "task_completion_rate": sum(s.task_completion for s in case_scores) / n,
        "answer_score_mean": sum(s.answer_score for s in case_scores) / n,
        "tool_call_accuracy_mean": sum(s.tool_call_accuracy for s in case_scores) / n,
        "elapsed_ms_mean": sum(s.elapsed_ms for s in case_scores) / n,
        "tool_path_applicable_count": float(len(applicable)),
    }
    if applicable:
        aggregate["tool_path_exact_rate"] = sum(
            s.tool_path_exact for s in applicable
        ) / len(applicable)
        aggregate["tool_path_equivalent_rate"] = sum(
            s.tool_path_equivalent for s in applicable
        ) / len(applicable)

    dimension_names = {d for s in case_scores for d in s.rubric_scores}
    for dimension in dimension_names:
        values = [
            s.rubric_scores[dimension]
            for s in case_scores
            if dimension in s.rubric_scores
        ]
        aggregate[f"{dimension}_mean"] = sum(values) / len(values)

    return aggregate


def run_evaluation(
    cases: list[Case],
    runtime: Runtime,
    *,
    limit: int | None = None,
    rubrics_dir: Path = DEFAULT_RUBRICS_DIR,
    prompts_dir: Path = hashing.DEFAULT_PROMPTS_DIR,
    models_config_path: Path = DEFAULT_MODELS_CONFIG_PATH,
) -> EvalRun:
    if limit is not None:
        cases = cases[:limit]

    rubrics = load_rubrics(rubrics_dir)

    case_scores: list[CaseScore] = []
    runtime_name = "unknown"
    mode = "single"
    for case in cases:
        score, agent_result = score_case(
            case, runtime, rubrics, models_config_path=models_config_path
        )
        case_scores.append(score)
        runtime_name, mode = agent_result.runtime, agent_result.mode

    return EvalRun(
        run_id=_run_id(),
        timestamp_utc=datetime.now(UTC),
        dataset=SOURCE,
        dataset_license=LICENSE,
        dataset_attribution=ATTRIBUTION,
        runtime=runtime_name,
        mode=mode,
        model_config_hash=hashing.compute_model_config_hash(models_config_path),
        prompt_hashes=hashing.compute_prompt_hashes(prompts_dir),
        rubric_version=RUBRIC_VERSION,
        case_scores=case_scores,
        aggregate=_aggregate(cases, case_scores),
        total_cost_eur=sum(s.cost_eur for s in case_scores),
    )
