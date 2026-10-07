"""Two eval runs side by side, with the gate's verdicts (#117).

Shared by `recon.cli compare` and `GET /evals/compare`, so the terminal and
the workspace can't disagree. Two runs that don't measure the same thing get
the reasons instead of verdicts (ADR 0018).
"""

from pydantic import BaseModel

from recon.contracts import EvalRun
from recon.eval.gate import comparability_failures, verdicts
from recon.eval.thresholds import GateThresholds

SETTINGS = ("runtime", "mode", "routing", "rubric_version", "model_config_hash")
# The metrics `gate.verdicts` judges.
_GATED = ("answer_score_mean", "task_completion_rate", "total_cost_eur")
# Shown next to the gated metrics, without a verdict: the gate doesn't judge them.
_REPORTED = (
    "cost_per_correct_answer_eur",
    "citation_precision_mean",
    "claim_support_rate_mean",
    "tool_call_accuracy_mean",
    "elapsed_ms_mean",
)


class SettingRow(BaseModel):
    """One setting of both runs."""

    name: str
    baseline: str
    candidate: str


class MetricRow(BaseModel):
    """One metric of both runs. `verdict` is "same", "better" or "worse" for a
    gated metric of comparable runs, and None otherwise."""

    name: str
    baseline: float | None
    candidate: float | None
    verdict: str | None = None
    # How far the metric may move and still count as "same", for gated metrics.
    band: str | None = None


class Comparison(BaseModel):
    """`candidate` judged against `baseline`."""

    baseline: str
    candidate: str
    comparable: bool
    reasons: list[str]
    settings: list[SettingRow]
    metrics: list[MetricRow]


def _setting(run: EvalRun, name: str) -> str:
    value = getattr(run, name)
    if name == "routing":
        return "on" if value else "off"
    return str(value)


def _metric(run: EvalRun, name: str) -> float | None:
    if name == "total_cost_eur":
        return run.total_cost_eur
    return run.aggregate.get(name)


def _bands(limits: GateThresholds) -> dict[str, str]:
    return {
        "answer_score_mean": f"±{limits.answer_score_noise_band:.2f}",
        "task_completion_rate": f"±{limits.task_completion_max_case_drop} case",
        "total_cost_eur": f"±{limits.cost_max_relative_rise:.0%}",
    }


def compare_runs(
    baseline: EvalRun, candidate: EvalRun, limits: GateThresholds
) -> Comparison:
    """The settings and metrics of both runs, with the gate's verdict per gated
    metric, or the reasons the gate refuses to compare them."""
    reasons = comparability_failures(
        rubric_version=candidate.rubric_version,
        dataset=candidate.dataset,
        tool_data_snapshot=candidate.tool_data_snapshot,
        case_ids=[score.case_id for score in candidate.case_scores],
        baseline=baseline,
    )
    judged = {} if reasons else verdicts(candidate, baseline, limits)
    bands = _bands(limits)
    metrics = [
        MetricRow(
            name=name,
            baseline=_metric(baseline, name),
            candidate=_metric(candidate, name),
            verdict=judged.get(name),
            band=bands.get(name),
        )
        for name in [*_GATED, *_REPORTED]
    ]
    return Comparison(
        baseline=baseline.run_id,
        candidate=candidate.run_id,
        comparable=not reasons,
        reasons=reasons,
        settings=[
            SettingRow(
                name=name,
                baseline=_setting(baseline, name),
                candidate=_setting(candidate, name),
            )
            for name in SETTINGS
        ],
        metrics=metrics,
    )
