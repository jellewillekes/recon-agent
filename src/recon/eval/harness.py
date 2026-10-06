"""Orchestrates a full evaluation run: score every case, hash prompts and
model config, assemble an `EvalRun`. See `docs/contracts.md` §7.
"""

from datetime import UTC, datetime
from pathlib import Path

import yaml

from recon.adapters.finance_agent_bench import ATTRIBUTION, DATASET_ID, LICENSE
from recon.contracts import AgentResult, Case, CaseScore, EvalRun
from recon.eval import faithfulness, hashing, metrics
from recon.eval.gate import SKIPPED_AT_COST_CAP
from recon.eval.judge import DEFAULT_MODELS_CONFIG_PATH, judge_case
from recon.eval.rubrics import DEFAULT_RUBRICS_DIR, Rubric, load_rubrics
from recon.runtimes.base import Runtime
from recon.tracing import record_agent_result, record_judge_call, span

# Bump when config/rubrics/*.yaml assertions change (docs/contracts.md §8:
# "Rubric changes are breaking: earlier runs are no longer comparable.").
# "2": answer_score became the weighted mean across dimensions (docs/adr/0014).
# "3": the judge runs on Haiku 4.5 instead of Sonnet 5 (docs/adr/0021). A
# different grader scores the same answer differently.
RUBRIC_VERSION = "3"

JUDGED_DESPITE_ERROR_NOTE = "answer judged despite runtime error"


def _run_id() -> str:
    return f"eval-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"


def _investigator_model(models_config_path: Path) -> str | None:
    config = yaml.safe_load(models_config_path.read_text(encoding="utf-8"))
    model = config.get("investigator", {}).get("model")
    return str(model) if model else None


def score_case(
    case: Case,
    runtime: Runtime,
    rubrics: dict[str, Rubric],
    *,
    models_config_path: Path = DEFAULT_MODELS_CONFIG_PATH,
    passages: faithfulness.Passages | None = None,
) -> tuple[CaseScore, AgentResult]:
    """Run, judge and score one case, under an `eval.case` trace span.

    With `passages`, an answer that used filing-text search is also scored
    for faithfulness to what the search returned."""
    with span("eval.case", **{"recon.case_id": case.case_id}) as case_span:
        score, agent_result = _score_case(
            case,
            runtime,
            rubrics,
            models_config_path=models_config_path,
            passages=passages,
        )
        case_span.set_attributes(
            {
                "recon.answer_score": score.answer_score,
                "recon.task_completion": score.task_completion,
                "recon.cost_eur": score.cost_eur,
            }
        )
    return score, agent_result


def _score_case(
    case: Case,
    runtime: Runtime,
    rubrics: dict[str, Rubric],
    *,
    models_config_path: Path,
    passages: faithfulness.Passages | None,
) -> tuple[CaseScore, AgentResult]:
    with span("invoke_agent") as agent_span:
        agent_result = runtime.run(case)
        # Multi mode picks a model per role (config/roles.yaml), so only a
        # single-mode run has one model to name.
        model = (
            _investigator_model(models_config_path)
            if agent_result.mode == "single"
            else None
        )
        record_agent_result(agent_span, agent_result, model)

    exact, exact_note = metrics.tool_path_exact(case, agent_result)
    equivalent, _ = metrics.tool_path_equivalent(case, agent_result)

    has_answer = bool(agent_result.answer.strip())
    notes_parts: list[str] = []
    if agent_result.error is not None:
        notes_parts.append(f"runtime error: {agent_result.error}")
        if has_answer:
            notes_parts.append(JUDGED_DESPITE_ERROR_NOTE)
    if exact_note is not None:
        notes_parts.append(exact_note)

    # Gate on the answer, not on `error`: runtimes also set `error` for
    # non-fatal outcomes that keep a complete answer (a token budget only
    # checked after the run finished, a LangGraph run paused before its
    # review-flag write). A real failure always returns an empty answer.
    # See docs/adr/0013.
    if not has_answer:
        rubric_scores = {dimension: 0.0 for dimension in rubrics}
        answer_score = 0.0
        judge_cost_eur = 0.0
    else:
        with span("chat judge") as judge_span:
            judge_result = judge_case(
                case, agent_result, rubrics, models_config_path=models_config_path
            )
            record_judge_call(
                judge_span,
                model=judge_result.model,
                tokens_in=judge_result.tokens_in,
                tokens_out=judge_result.tokens_out,
                num_turns=judge_result.num_turns,
                cost_eur=judge_result.cost_eur,
            )
        rubric_scores = judge_result.rubric_scores
        answer_score = metrics.weighted_answer_score(rubric_scores, rubrics)
        judge_cost_eur = judge_result.cost_eur
        if passages is not None:
            faithful = _judge_faithfulness(
                case, agent_result, passages, models_config_path
            )
            if faithful.score is not None:
                # Not a rubric, so weighted_answer_score has already ignored it.
                rubric_scores = {
                    **rubric_scores,
                    faithfulness.DIMENSION: faithful.score,
                }
            judge_cost_eur += faithful.cost_eur

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
        tool_names=[call.tool for call in agent_result.tool_calls],
    )
    return score, agent_result


def _judge_faithfulness(
    case: Case,
    agent_result: AgentResult,
    passages: faithfulness.Passages,
    models_config_path: Path,
) -> faithfulness.FaithfulnessResult:
    """`faithfulness.judge_faithfulness` under its own judge trace span."""
    with span("chat judge faithfulness") as judge_span:
        result = faithfulness.judge_faithfulness(
            case, agent_result, passages, models_config_path=models_config_path
        )
        record_judge_call(
            judge_span,
            model=result.model,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            num_turns=result.num_turns,
            cost_eur=result.cost_eur,
        )
    return result


def _aggregate(
    cases: list[Case], case_scores: list[CaseScore], agent_cost_eur: float
) -> dict[str, float]:
    n = len(case_scores)
    if n == 0:
        return {}
    total_cost_eur = sum(s.cost_eur for s in case_scores)

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
        # Which side of a case the credit went to, so cost decisions rest on
        # measurements (the judge model, say) rather than estimates.
        "agent_cost_eur": agent_cost_eur,
        "judge_cost_eur": total_cost_eur - agent_cost_eur,
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
        if dimension == faithfulness.DIMENSION:
            # Scored only on cases that searched, so the mean may rest on few.
            aggregate["faithfulness_scored_cases"] = float(len(values))

    return aggregate


def _over_budget(case_scores: list[CaseScore], max_cost_eur: float | None) -> bool:
    """Whether the next case could take the run past `max_cost_eur`.

    The next case is assumed to cost as much as the dearest one so far. The
    first case always runs, since nothing is known yet; a single case is
    bounded by `run_budget` in config/models.yaml.
    """
    if max_cost_eur is None or not case_scores:
        return False
    spent = sum(s.cost_eur for s in case_scores)
    return spent + max(s.cost_eur for s in case_scores) > max_cost_eur


def run_evaluation(
    cases: list[Case],
    runtime: Runtime,
    *,
    limit: int | None = None,
    rubrics_dir: Path = DEFAULT_RUBRICS_DIR,
    prompts_dir: Path = hashing.DEFAULT_PROMPTS_DIR,
    models_config_path: Path = DEFAULT_MODELS_CONFIG_PATH,
    # Only read (and hashed into model_config_hash) when the run turns out to
    # be mode="multi" - config/roles.yaml governs each role's model/max_turns
    # there, same reproducibility stakes as models.yaml (docs/contracts.md
    # §7). A literal default, not an import from runtimes.multi_agent: the
    # harness depends only on the Runtime protocol, never a specific runtime
    # module (see this module's docstring).
    roles_config_path: Path = Path("config/roles.yaml"),
    # Which tool data the runtime's MCP server queries, from
    # tools.data_source.tool_data_snapshot_id(). Passed in rather than
    # looked up here, so the harness depends on no tool module. None records
    # "unknown", which the promotion gate refuses to compare.
    tool_data_snapshot: str | None = None,
    # Stop before a case that could take the run past this many euros. None
    # means no cap. See _over_budget for how "could" is judged.
    max_cost_eur: float | None = None,
    # Replays filing-text searches for the faithfulness score. Passed in, like
    # tool_data_snapshot, so the harness depends on no tool module. None skips
    # faithfulness.
    passages: faithfulness.Passages | None = None,
) -> EvalRun:
    if limit is not None:
        cases = cases[:limit]

    rubrics = load_rubrics(rubrics_dir)
    with span("eval.run", **{"recon.cases_requested": len(cases)}) as run_span:
        run = _evaluate(
            cases,
            runtime,
            rubrics,
            prompts_dir=prompts_dir,
            models_config_path=models_config_path,
            roles_config_path=roles_config_path,
            tool_data_snapshot=tool_data_snapshot,
            max_cost_eur=max_cost_eur,
            passages=passages,
        )
        run_span.set_attributes(
            {
                "recon.run_id": run.run_id,
                "recon.runtime": run.runtime,
                "recon.mode": run.mode,
                "recon.total_cost_eur": run.total_cost_eur,
                **{f"recon.{key}": value for key, value in run.aggregate.items()},
            }
        )
    return run


def _run_cases(
    cases: list[Case],
    runtime: Runtime,
    rubrics: dict[str, Rubric],
    *,
    models_config_path: Path,
    max_cost_eur: float | None,
    passages: faithfulness.Passages | None,
) -> tuple[list[CaseScore], float, str, str]:
    """Score cases until done or the cost cap is near. Returns the scores, the
    agent's share of the cost, and the runtime and mode that ran them."""
    case_scores: list[CaseScore] = []
    agent_cost_eur = 0.0
    runtime_name = "unknown"
    mode = "single"
    for case in cases:
        if _over_budget(case_scores, max_cost_eur):
            break
        score, agent_result = score_case(
            case,
            runtime,
            rubrics,
            models_config_path=models_config_path,
            passages=passages,
        )
        case_scores.append(score)
        agent_cost_eur += agent_result.cost_eur
        runtime_name, mode = agent_result.runtime, agent_result.mode
    return case_scores, agent_cost_eur, runtime_name, mode


def _evaluate(
    cases: list[Case],
    runtime: Runtime,
    rubrics: dict[str, Rubric],
    *,
    prompts_dir: Path,
    models_config_path: Path,
    roles_config_path: Path,
    tool_data_snapshot: str | None,
    max_cost_eur: float | None,
    passages: faithfulness.Passages | None,
) -> EvalRun:
    case_scores, agent_cost_eur, runtime_name, mode = _run_cases(
        cases,
        runtime,
        rubrics,
        models_config_path=models_config_path,
        max_cost_eur=max_cost_eur,
        passages=passages,
    )
    run_cases = cases[: len(case_scores)]
    aggregate = _aggregate(run_cases, case_scores, agent_cost_eur)
    if len(case_scores) < len(cases):
        aggregate[SKIPPED_AT_COST_CAP] = float(len(cases) - len(case_scores))

    model_config_hash = (
        hashing.compute_model_config_hash(models_config_path, roles_config_path)
        if mode == "multi"
        else hashing.compute_model_config_hash(models_config_path)
    )

    return EvalRun(
        run_id=_run_id(),
        timestamp_utc=datetime.now(UTC),
        dataset=DATASET_ID,
        dataset_license=LICENSE,
        dataset_attribution=ATTRIBUTION,
        runtime=runtime_name,
        mode=mode,
        model_config_hash=model_config_hash,
        prompt_hashes=hashing.compute_prompt_hashes(prompts_dir),
        rubric_version=RUBRIC_VERSION,
        case_scores=case_scores,
        aggregate=aggregate,
        total_cost_eur=sum(s.cost_eur for s in case_scores),
        tool_data_snapshot=tool_data_snapshot,
    )


def with_cost_per_correct_answer(
    run: EvalRun, correct_answer_score: float | None
) -> EvalRun:
    """`run` with `cost_per_correct_answer_eur` in its aggregate (#77).

    Derived from the per-case scores, so it can be added to any run once the
    cutoff is set. Unchanged when the cutoff is None or no case is correct.
    """
    if correct_answer_score is None:
        return run
    value = metrics.cost_per_correct_answer_eur(run.case_scores, correct_answer_score)
    if value is None:
        return run
    aggregate = {**run.aggregate, "cost_per_correct_answer_eur": value}
    return run.model_copy(update={"aggregate": aggregate})
