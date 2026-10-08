"""Two eval runs side by side, with the gate's verdicts (#117).

Shared by `recon.cli compare` and `GET /evals/compare`, so the terminal and
the workspace can't disagree. Two runs that don't measure the same thing get
the reasons instead of verdicts (ADR 0018).
"""

from pydantic import BaseModel

from recon.contracts import EvalRun
from recon.eval.claim_gate import skip_note
from recon.eval.gate import (
    comparability_failures,
    incomplete_run_failures,
    noise_rule,
    verdicts,
)
from recon.eval.intervals import Interval, run_interval
from recon.eval.noise import estimate_noise, settings_key
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
    # 95% interval over each run's cases, where the metric has one (ADR 0036).
    baseline_interval: Interval | None = None
    candidate_interval: Interval | None = None


class Comparison(BaseModel):
    """`candidate` judged against `baseline`."""

    baseline: str
    candidate: str
    comparable: bool
    reasons: list[str]
    settings: list[SettingRow]
    metrics: list[MetricRow]
    # Which noise rule the gate applied, and what the stored runs say about
    # run-to-run noise. Neither changes a verdict.
    noise_rule: str = ""
    warnings: list[str] = []


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


def noise_warnings(
    baseline: EvalRun, history: list[EvalRun], limits: GateThresholds
) -> list[str]:
    """What stored repeat runs of the baseline's settings say about the gate's
    answer score band: nothing measured, or noise wider than the band."""
    key = settings_key(baseline)
    repeats = [baseline] + [
        run
        for run in history
        if run.run_id != baseline.run_id and settings_key(run) == key
    ]
    band = limits.answer_score_noise_band
    if len(repeats) < 2:
        return [
            (
                "Run-to-run noise on the baseline's settings is unmeasured: no "
                f"repeat run of them is stored. The gate's band of ±{band:.2f} "
                "is a judgement (ADR 0028)."
            )
        ]
    noise = estimate_noise(repeats)
    if noise.band_95 <= band:
        return []
    return [
        (
            "Measured run-to-run noise on the baseline's settings is "
            f"±{noise.band_95:.2f} at 95% ({noise.runs} runs, {noise.cases} cases), "
            f"wider than the gate's ±{band:.2f}. A drop of {band:.2f} can be noise, "
            "and the gate may fail an unchanged candidate."
        )
    ]


def _warnings(
    baseline: EvalRun, history: list[EvalRun], limits: GateThresholds
) -> list[str]:
    note = skip_note(baseline)
    return noise_warnings(baseline, history, limits) + ([note] if note else [])


def compare_runs(
    baseline: EvalRun,
    candidate: EvalRun,
    limits: GateThresholds,
    *,
    correct_answer_score: float | None = None,
    history: list[EvalRun] | None = None,
) -> Comparison:
    """The settings and metrics of both runs, with the gate's verdict per gated
    metric, or the reasons the gate refuses to compare them.

    `history` is every stored run, for measuring noise; `correct_answer_score`
    is the cutoff for cost per correct answer's interval.
    """
    reasons = comparability_failures(
        rubric_version=candidate.rubric_version,
        dataset=candidate.dataset,
        tool_data_snapshot=candidate.tool_data_snapshot,
        case_ids=[score.case_id for score in candidate.case_scores],
        baseline=baseline,
    ) + incomplete_run_failures(candidate, "candidate")
    judged = {} if reasons else verdicts(candidate, baseline, limits)
    bands = _bands(limits)
    metrics = [
        MetricRow(
            name=name,
            baseline=_metric(baseline, name),
            candidate=_metric(candidate, name),
            verdict=judged.get(name),
            band=bands.get(name),
            baseline_interval=run_interval(baseline, name, correct_answer_score),
            candidate_interval=run_interval(candidate, name, correct_answer_score),
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
        noise_rule=noise_rule(limits),
        warnings=_warnings(baseline, history or [], limits),
    )
