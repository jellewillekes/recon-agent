"""`recon.cli compare`: two eval runs side by side (step 14, #19).

Shows each run's settings and the gated metrics, with the gate's verdict for
the second run against the first. Used to compare routing on against off; any
two comparable runs work. Reads result files only, so it costs nothing.
"""

import argparse
from pathlib import Path

from recon.contracts import EvalRun
from recon.eval.gate import comparability_failures, verdicts
from recon.eval.thresholds import load_thresholds

_SETTINGS = ("runtime", "mode", "routing", "model_config_hash")


def _setting(run: EvalRun, name: str) -> str:
    value = getattr(run, name)
    if name == "routing":
        return "on" if value else "off"
    return str(value)


def _cmd_compare(args: argparse.Namespace) -> None:
    first = EvalRun.model_validate_json(args.first.read_text(encoding="utf-8"))
    second = EvalRun.model_validate_json(args.second.read_text(encoding="utf-8"))
    print(f"| | {first.run_id} | {second.run_id} | verdict |")
    print("|---|---|---|---|")
    for name in _SETTINGS:
        print(f"| {name} | {_setting(first, name)} | {_setting(second, name)} | |")
    failures = comparability_failures(
        rubric_version=second.rubric_version,
        dataset=second.dataset,
        tool_data_snapshot=second.tool_data_snapshot,
        case_ids=[score.case_id for score in second.case_scores],
        baseline=first,
    )
    judged = {} if failures else verdicts(second, first, load_thresholds().gate)
    for metric in ("answer_score_mean", "task_completion_rate"):
        print(
            f"| {metric} | {first.aggregate.get(metric, 0.0):.3f} | "
            f"{second.aggregate.get(metric, 0.0):.3f} | {judged.get(metric, '')} |"
        )
    print(
        f"| total_cost_eur | {first.total_cost_eur:.3f} | {second.total_cost_eur:.3f} "
        f"| {judged.get('total_cost_eur', '')} |"
    )
    for failure in failures:
        print(f"Not comparable: {failure}")


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
