"""`recon.cli compare`: two eval runs side by side (step 14, #19).

Shows each run's settings and metrics, with the gate's verdict for the second
run against the first (`eval/comparison.py`, shared with `GET /evals/compare`). Used to compare routing on against off; any
two comparable runs work. Values show their 95% interval in brackets (ADR 0036).
Reads result files only, so it costs nothing.
"""

import argparse
from pathlib import Path

from pydantic import ValidationError

from recon.contracts import EvalRun
from recon.eval.comparison import compare_runs
from recon.eval.intervals import Interval
from recon.eval.thresholds import load_thresholds

RESULTS_DIR = Path("evals/results")


def _value(value: float | None, interval: Interval | None = None) -> str:
    if value is None:
        return "—"
    if interval is None:
        return f"{value:.3f}"
    return f"{value:.3f} ({interval.low:.2f} to {interval.high:.2f})"


def _stored_runs() -> list[EvalRun]:
    """Every readable run in `evals/results/`, for measuring noise."""
    runs = []
    for path in sorted(RESULTS_DIR.glob("eval-*.json")):
        try:
            runs.append(EvalRun.model_validate_json(path.read_text(encoding="utf-8")))
        except (OSError, ValidationError):
            continue
    return runs


def _cmd_compare(args: argparse.Namespace) -> None:
    first = EvalRun.model_validate_json(args.first.read_text(encoding="utf-8"))
    second = EvalRun.model_validate_json(args.second.read_text(encoding="utf-8"))
    thresholds = load_thresholds()
    result = compare_runs(
        first,
        second,
        thresholds.gate,
        correct_answer_score=thresholds.correct_answer_score,
        history=_stored_runs(),
    )
    print(f"| | {result.baseline} | {result.candidate} | verdict |")
    print("|---|---|---|---|")
    for setting in result.settings:
        print(f"| {setting.name} | {setting.baseline} | {setting.candidate} | |")
    for metric in result.metrics:
        print(
            f"| {metric.name} | "
            f"{_value(metric.baseline, metric.baseline_interval)} | "
            f"{_value(metric.candidate, metric.candidate_interval)} | "
            f"{metric.verdict or ''} |"
        )
    for reason in result.reasons:
        print(f"Not comparable: {reason}")
    print(f"Noise rule: {result.noise_rule}")
    for warning in result.warnings:
        print(f"Warning: {warning}")


def add_compare_parser(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> None:
    """Register `compare` on the top-level parser."""
    parser = subparsers.add_parser(
        "compare", help="Two eval runs side by side, with the gate's verdicts."
    )
    parser.add_argument("first", type=Path, help="The run to compare against.")
    parser.add_argument("second", type=Path, help="The run being judged.")
    parser.set_defaults(func=_cmd_compare)
