"""Promotion gate: `docs/contracts.md` §9.

Compares a candidate `EvalRun`'s aggregate metrics against a baseline
`EvalRun` and returns the rules that failed. Empty means the candidate is
promotable.
"""

from recon.contracts import EvalRun

ANSWER_SCORE_DROP_THRESHOLD = 0.02  # 2%
COST_RISE_THRESHOLD = 0.20  # 20%


def check_gate(candidate: EvalRun, baseline: EvalRun) -> list[str]:
    failures: list[str] = []

    candidate_completion = candidate.aggregate.get("task_completion_rate", 0.0)
    baseline_completion = baseline.aggregate.get("task_completion_rate", 0.0)
    if candidate_completion < baseline_completion:
        failures.append(
            "task_completion_rate dropped: "
            f"{baseline_completion:.3f} -> {candidate_completion:.3f}"
        )

    candidate_answer = candidate.aggregate.get("answer_score_mean", 0.0)
    baseline_answer = baseline.aggregate.get("answer_score_mean", 0.0)
    # "Weighted answer_score" per docs/contracts.md §9 — answer_score is
    # already the single answer_correctness dimension per case (judge.py),
    # so this is the plain mean across cases; there's no further per-case
    # weighting defined to apply on top of that.
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
