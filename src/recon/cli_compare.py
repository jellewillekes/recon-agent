"""`recon.cli compare`: two eval runs side by side (step 14, #19).

Shows each run's settings and metrics, with the gate's verdict for the second
run against the first (`eval/comparison.py`, shared with `GET /evals/compare`). Used to compare routing on against off; any
two comparable runs work. Reads result files only, so it costs nothing.
"""

import argparse
from pathlib import Path

from recon.contracts import EvalRun
from recon.eval.comparison import compare_runs
from recon.eval.thresholds import load_thresholds


def _value(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}"


def _cmd_compare(args: argparse.Namespace) -> None:
    first = EvalRun.model_validate_json(args.first.read_text(encoding="utf-8"))
    second = EvalRun.model_validate_json(args.second.read_text(encoding="utf-8"))
    result = compare_runs(first, second, load_thresholds().gate)
    print(f"| | {result.baseline} | {result.candidate} | verdict |")
    print("|---|---|---|---|")
    for setting in result.settings:
        print(f"| {setting.name} | {setting.baseline} | {setting.candidate} | |")
    for metric in result.metrics:
        print(
            f"| {metric.name} | {_value(metric.baseline)} | "
            f"{_value(metric.candidate)} | {metric.verdict or ''} |"
        )
    for reason in result.reasons:
        print(f"Not comparable: {reason}")


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
