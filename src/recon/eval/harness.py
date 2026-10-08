"""Orchestrates a full evaluation run: score every case, hash prompts and
model config, assemble an `EvalRun`. See `docs/contracts.md` §7.
"""

from datetime import UTC, datetime
from pathlib import Path

from recon.adapters.finance_agent_bench import ATTRIBUTION, DATASET_ID, LICENSE
from recon.contracts import Case, CaseScore, EvalRun
from recon.eval import claim_replay, faithfulness, hashing, metrics
from recon.eval.aggregate import aggregate_scores
from recon.eval.claim_verifier import verifier_version
from recon.eval.gate import SKIPPED_AT_COST_CAP, SKIPPED_AT_SESSION_LIMIT
from recon.eval.judge import DEFAULT_MODELS_CONFIG_PATH
from recon.eval.judge_failures import (
    SessionLimitReached,
)
from recon.eval.rubrics import DEFAULT_RUBRICS_DIR, Rubric, load_rubrics
from recon.eval.scoring import score_case
from recon.runtimes.base import Runtime
from recon.tracing import span

# Bump when config/rubrics/*.yaml assertions change (docs/contracts.md §8:
# "Rubric changes are breaking: earlier runs are no longer comparable.").
# "2": answer_score became the weighted mean across dimensions (docs/adr/0014).
# "3": the judge runs on Haiku 4.5 instead of Sonnet 5 (docs/adr/0021). A
# different grader scores the same answer differently.
# "4": answers are claims citing row refs, and the judges see the resolved
# evidence instead of the model's own strings (docs/adr/0030).
RUBRIC_VERSION = "4"


def _run_id() -> str:
    return f"eval-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"


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
    # Relevant chunk ids per case, for each case's retrieval score (#116).
    # Used only with `passages`, which replays the agent's searches.
    retrieval_labels: dict[str, list[str]] | None = None,
    # The answer_score a correct answer reaches, from config/thresholds.yaml.
    # None leaves judged cases without a failure class.
    correct_answer_score: float | None = None,
    # Replays the agent's fact calls to verify its claims (#139, ADR 0038).
    # Passed in, like `passages`. None leaves claims unverified.
    facts: claim_replay.Facts | None = None,
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
            retrieval_labels=retrieval_labels,
            correct_answer_score=correct_answer_score,
            facts=facts,
        )
        run_span.set_attributes(_run_attributes(run))
    return run


def _run_attributes(run: EvalRun) -> dict[str, str | float]:
    return {
        "recon.run_id": run.run_id,
        "recon.runtime": run.runtime,
        "recon.mode": run.mode,
        "recon.total_cost_eur": run.total_cost_eur,
        **{f"recon.{key}": value for key, value in run.aggregate.items()},
    }


def _run_cases(
    cases: list[Case],
    runtime: Runtime,
    rubrics: dict[str, Rubric],
    *,
    models_config_path: Path,
    max_cost_eur: float | None,
    passages: faithfulness.Passages | None,
    retrieval_labels: dict[str, list[str]] | None,
    correct_answer_score: float | None,
    facts: claim_replay.Facts | None,
) -> tuple[list[CaseScore], float, str, str, bool]:
    """Score cases until done, the cost cap is near, or the session limit is
    hit. Returns the scores, the agent's share of the cost, the runtime and
    mode that ran them, and whether the session limit stopped the run."""
    case_scores: list[CaseScore] = []
    agent_cost_eur = 0.0
    runtime_name = "unknown"
    mode = "single"
    session_limit = False
    for case in cases:
        if session_limit or _over_budget(case_scores, max_cost_eur):
            break
        try:
            score, agent_result = score_case(
                case,
                runtime,
                rubrics,
                models_config_path=models_config_path,
                passages=passages,
                retrieval_labels=retrieval_labels,
                correct_answer_score=correct_answer_score,
                facts=facts,
            )
        except SessionLimitReached as reached:
            score, agent_result = reached.score, reached.agent_result
            session_limit = True
        case_scores.append(score)
        agent_cost_eur += agent_result.cost_eur
        runtime_name, mode = agent_result.runtime, agent_result.mode
        status = "completed" if score.task_completion else "not completed"
        print(
            f"case {len(case_scores)}/{len(cases)} {case.case_id}: {status}, "
            f"answer {score.answer_score:.2f}, {score.elapsed_ms / 1000:.0f} s, "
            f"€{score.cost_eur:.2f}",
            flush=True,
        )
    if session_limit and len(case_scores) < len(cases):
        print("Stopped: the subscription's session limit was hit.", flush=True)
    return case_scores, agent_cost_eur, runtime_name, mode, session_limit


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
    retrieval_labels: dict[str, list[str]] | None,
    correct_answer_score: float | None,
    facts: claim_replay.Facts | None,
) -> EvalRun:
    case_scores, agent_cost_eur, runtime_name, mode, session_limit = _run_cases(
        cases,
        runtime,
        rubrics,
        models_config_path=models_config_path,
        max_cost_eur=max_cost_eur,
        passages=passages,
        retrieval_labels=retrieval_labels,
        correct_answer_score=correct_answer_score,
        facts=facts,
    )
    aggregate = {
        **aggregate_scores(cases[: len(case_scores)], case_scores, agent_cost_eur),
        **_skip_markers(len(cases), len(case_scores), session_limit),
    }

    # Multi mode also reads config/roles.yaml, so it's part of the hash.
    roles = [roles_config_path] if mode == "multi" else []
    model_config_hash = hashing.compute_model_config_hash(models_config_path, *roles)

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
        verifier_version=verifier_version() if facts is not None else None,
    )


def _skip_markers(requested: int, scored: int, session_limit: bool) -> dict[str, float]:
    """The aggregate marker for cases the run didn't measure. A session limit
    also counts the case it cut short, which is the last one scored (#123)."""
    if session_limit:
        return {SKIPPED_AT_SESSION_LIMIT: float(requested - scored + 1)}
    if scored < requested:
        return {SKIPPED_AT_COST_CAP: float(requested - scored)}
    return {}


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
