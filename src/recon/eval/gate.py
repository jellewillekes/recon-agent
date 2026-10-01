"""Promotion gate: `docs/contracts.md` §9.

Compares a candidate `EvalRun`'s aggregate metrics against a baseline
`EvalRun` and returns the rules that failed. Empty means the candidate is
promotable. Runs that don't measure the same thing are refused before any
metric is compared; see docs/adr/0018-run-comparability-in-the-gate.md.
"""

from collections.abc import Collection

from recon.contracts import EvalRun

ANSWER_SCORE_DROP_THRESHOLD = 0.02  # 2%
COST_RISE_THRESHOLD = 0.20  # 20%

# Set in `EvalRun.aggregate` when the cost cap stopped a run before its last
# case. Such a run didn't measure its whole case set, so the gate refuses it
# as a candidate and as a baseline.
SKIPPED_AT_COST_CAP = "cases_skipped_at_cost_cap"


def comparability_failures(
    *,
    rubric_version: str,
    dataset: str,
    tool_data_snapshot: str | None,
    case_ids: Collection[str],
    baseline: EvalRun,
) -> list[str]:
    """Why a candidate with these properties can't be compared with `baseline`.

    Takes plain values rather than an `EvalRun` so `recon.cli eval` can check
    before a run starts, instead of spending credit on a run the gate is
    bound to refuse.
    """
    regenerate = "Regenerate the baseline through an explicit PR."
    failures: list[str] = []
    if rubric_version != baseline.rubric_version:
        failures.append(
            f"rubric_version differs: baseline {baseline.rubric_version!r}, "
            f"candidate {rubric_version!r}. Scores aren't comparable. {regenerate}"
        )
    if dataset != baseline.dataset:
        failures.append(
            f"dataset differs: baseline {baseline.dataset!r}, candidate {dataset!r}. "
            f"A new dataset or pin needs a new baseline. {regenerate}"
        )
    if baseline.tool_data_snapshot is None:
        failures.append(
            "the baseline doesn't record which tool data it queried "
            f"(tool_data_snapshot). {regenerate}"
        )
    elif tool_data_snapshot != baseline.tool_data_snapshot:
        failures.append(
            f"tool_data_snapshot differs: baseline {baseline.tool_data_snapshot!r}, "
            f"candidate {tool_data_snapshot!r}. Use the baseline's snapshot, or "
            "regenerate the baseline through an explicit PR."
        )
    skipped = baseline.aggregate.get(SKIPPED_AT_COST_CAP)
    if skipped:
        failures.append(
            f"the baseline run stopped at its cost cap with {skipped:.0f} case(s) "
            f"not run, so it didn't measure its whole case set. {regenerate}"
        )
    baseline_ids = {s.case_id for s in baseline.case_scores}
    if set(case_ids) != baseline_ids:
        missing, extra = baseline_ids - set(case_ids), set(case_ids) - baseline_ids
        failures.append(
            f"case sets differ: {len(missing)} baseline case(s) not run, "
            f"{len(extra)} case(s) not in the baseline. Rerun on the baseline's "
            f"{len(baseline_ids)} cases (a --limit run can't be compared with a "
            "full baseline)."
        )
    return failures


def check_gate(candidate: EvalRun, baseline: EvalRun) -> list[str]:
    failures = comparability_failures(
        rubric_version=candidate.rubric_version,
        dataset=candidate.dataset,
        tool_data_snapshot=candidate.tool_data_snapshot,
        case_ids=[s.case_id for s in candidate.case_scores],
        baseline=baseline,
    )
    # The baseline side is in comparability_failures, so the CLI refuses a
    # capped baseline before running. A candidate is only known afterwards.
    skipped = candidate.aggregate.get(SKIPPED_AT_COST_CAP)
    if skipped:
        failures.append(
            f"the candidate run stopped at its cost cap with {skipped:.0f} case(s) "
            "not run. Rerun it with a higher --max-cost-eur or fewer cases."
        )
    if failures:
        return failures

    candidate_completion = candidate.aggregate.get("task_completion_rate", 0.0)
    baseline_completion = baseline.aggregate.get("task_completion_rate", 0.0)
    if candidate_completion < baseline_completion:
        failures.append(
            "task_completion_rate dropped: "
            f"{baseline_completion:.3f} -> {candidate_completion:.3f}"
        )

    candidate_answer = candidate.aggregate.get("answer_score_mean", 0.0)
    baseline_answer = baseline.aggregate.get("answer_score_mean", 0.0)
    # "Weighted answer_score" per docs/contracts.md §9. Each case's
    # answer_score is already weighted across rubric dimensions
    # (metrics.weighted_answer_score), so this is the plain mean of that.
    if baseline_answer > 0:
        relative_drop = (baseline_answer - candidate_answer) / baseline_answer
        if relative_drop > ANSWER_SCORE_DROP_THRESHOLD:
            failures.append(
                f"answer_score_mean dropped more than {ANSWER_SCORE_DROP_THRESHOLD:.0%}: "
                f"{baseline_answer:.3f} -> {candidate_answer:.3f}"
            )

    if baseline.total_cost_eur > 0:
        cost_rise = (
            candidate.total_cost_eur - baseline.total_cost_eur
        ) / baseline.total_cost_eur
        if (
            cost_rise > COST_RISE_THRESHOLD
            and candidate_completion <= baseline_completion
        ):
            failures.append(
                f"total_cost_eur rose more than {COST_RISE_THRESHOLD:.0%} without a rise "
                f"in task completion: €{baseline.total_cost_eur:.4f} -> "
                f"€{candidate.total_cost_eur:.4f}"
            )

    return failures
