"""Promotion gate: `docs/contracts.md` §9.

Compares a candidate `EvalRun`'s aggregate metrics against a baseline
`EvalRun` and returns the rules that failed. Empty means the candidate is
promotable. Runs that don't measure the same thing are refused before any
metric is compared; see docs/adr/0018-run-comparability-in-the-gate.md.
"""

from collections.abc import Collection

from recon.contracts import EvalRun
from recon.eval.claim_gate import claim_failures, version_failures
from recon.eval.thresholds import GateThresholds

# Set in `EvalRun.aggregate` when the cost cap stopped a run before its last
# case. Such a run didn't measure its whole case set, so the gate refuses it
# as a candidate and as a baseline.
SKIPPED_AT_COST_CAP = "cases_skipped_at_cost_cap"
# Cases the subscription's session limit cut short or kept from running (#123).
# Refused the same way as a capped run.
SKIPPED_AT_SESSION_LIMIT = "cases_skipped_at_session_limit"
# Cases whose rubric judge call failed (#123). Their answer_score is a
# placeholder 0.0, not a measurement, so the run is refused the same way.
CASES_JUDGE_FAILED = "cases_judge_failed"

# Aggregate key -> how a run with that many cases unmeasured is described.
_INCOMPLETE_RUN = {
    SKIPPED_AT_COST_CAP: "stopped at its cost cap with {n} case(s) not run",
    SKIPPED_AT_SESSION_LIMIT: (
        "stopped at the session limit with {n} case(s) cut short or not run"
    ),
    CASES_JUDGE_FAILED: "has {n} case(s) the judge couldn't score",
}


def incomplete_reasons(run: EvalRun) -> list[str]:
    """What `run` left unmeasured, one line per marker in its aggregate.
    Empty for a complete run."""
    return [
        template.format(n=f"{count:.0f}")
        for key, template in _INCOMPLETE_RUN.items()
        if (count := run.aggregate.get(key))
    ]


def incomplete_run_failures(run: EvalRun, side: str) -> list[str]:
    """Why `run` didn't measure its whole case set, as gate failures naming
    `side` ("baseline" or "candidate"). Empty for a complete run."""
    rerun = (
        "Regenerate the baseline through an explicit PR."
        if side == "baseline"
        else "Rerun it once the cause is gone: a higher --max-cost-eur, fewer "
        "cases, or after the session limit resets."
    )
    return [
        f"the {side} run {reason}, so it didn't measure its whole case set. {rerun}"
        for reason in incomplete_reasons(run)
    ]


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
    failures.extend(incomplete_run_failures(baseline, "baseline"))
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


def check_gate(
    candidate: EvalRun, baseline: EvalRun, limits: GateThresholds
) -> list[str]:
    """The rules `candidate` fails against `baseline`. Empty means promotable.

    `limits` is the `gate` section of config/thresholds.yaml. Callers pass it,
    so the gate itself reads no file.
    """
    failures = comparability_failures(
        rubric_version=candidate.rubric_version,
        dataset=candidate.dataset,
        tool_data_snapshot=candidate.tool_data_snapshot,
        case_ids=[s.case_id for s in candidate.case_scores],
        baseline=baseline,
    )
    # The baseline side is in comparability_failures, so the CLI refuses a
    # capped baseline before running. A candidate is only known afterwards.
    failures.extend(incomplete_run_failures(candidate, "candidate"))
    failures.extend(version_failures(candidate, baseline))
    if failures:
        return failures

    candidate_completion = candidate.aggregate.get("task_completion_rate", 0.0)
    baseline_completion = baseline.aggregate.get("task_completion_rate", 0.0)
    judged = verdicts(candidate, baseline, limits)
    if judged["task_completion_rate"] == "worse":
        failures.append(
            "task_completion_rate dropped by more than "
            f"{limits.task_completion_max_case_drop} case(s): "
            f"{baseline_completion:.3f} -> {candidate_completion:.3f}"
        )
    # "Weighted answer_score" per docs/contracts.md §9. Each case's
    # answer_score is already weighted across rubric dimensions
    # (metrics.weighted_answer_score), so this is the plain mean of that.
    if judged["answer_score_mean"] == "worse":
        failures.append(
            "answer_score_mean dropped by more than the noise band "
            f"({limits.answer_score_noise_band:.2f}): "
            f"{baseline.aggregate.get('answer_score_mean', 0.0):.3f} -> "
            f"{candidate.aggregate.get('answer_score_mean', 0.0):.3f}"
        )

    if baseline.total_cost_eur > 0:
        cost_rise = (
            candidate.total_cost_eur - baseline.total_cost_eur
        ) / baseline.total_cost_eur
        if (
            cost_rise > limits.cost_max_relative_rise
            and candidate_completion <= baseline_completion
        ):
            failures.append(
                f"total_cost_eur rose more than {limits.cost_max_relative_rise:.0%} "
                "without a rise "
                f"in task completion: €{baseline.total_cost_eur:.4f} -> "
                f"€{candidate.total_cost_eur:.4f}"
            )

    failures.extend(claim_failures(candidate, baseline, limits))
    return failures


def noise_rule(limits: GateThresholds) -> str:
    """Which noise rule the gate applies, in words (ADR 0028, 0036). It is the
    fixed band from the thresholds file, not one derived from the data."""
    return (
        f"answer score: fixed band ±{limits.answer_score_noise_band:.2f}; "
        f"task completion: {limits.task_completion_max_case_drop} case(s); "
        f"cost: ±{limits.cost_max_relative_rise:.0%}. "
        "Set in config/thresholds.yaml, not derived from the data (ADR 0028)."
    )


def _change(delta: float, tolerance: float, higher_is_better: bool) -> str:
    # The epsilon keeps a drop of exactly the tolerance, such as one case of
    # seven, from failing on float rounding.
    if abs(delta) <= tolerance + 1e-9:
        return "same"
    return "better" if (delta > 0) == higher_is_better else "worse"


def verdicts(
    candidate: EvalRun, baseline: EvalRun, limits: GateThresholds
) -> dict[str, str]:
    """ "same", "better" or "worse" per gated metric (#77).

    A change within the measured run-to-run noise is "same": the answer score
    within `answer_score_noise_band`, task completion within
    `task_completion_max_case_drop` cases, and cost within
    `cost_max_relative_rise` of the baseline's. A "worse" cost still passes
    `check_gate` when task completion rose: more cases answered may cost more.
    """
    cases = len(baseline.case_scores) or int(baseline.aggregate.get("case_count", 0))
    completion_band = limits.task_completion_max_case_drop / cases if cases else 0.0
    cost_change = (
        (candidate.total_cost_eur - baseline.total_cost_eur) / baseline.total_cost_eur
        if baseline.total_cost_eur > 0
        else 0.0
    )

    def delta(metric: str) -> float:
        return candidate.aggregate.get(metric, 0.0) - baseline.aggregate.get(
            metric, 0.0
        )

    return {
        "answer_score_mean": _change(
            delta("answer_score_mean"), limits.answer_score_noise_band, True
        ),
        "task_completion_rate": _change(
            delta("task_completion_rate"), completion_band, True
        ),
        "total_cost_eur": _change(cost_change, limits.cost_max_relative_rise, False),
    }
