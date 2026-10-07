#!/usr/bin/env python3
"""Check the committed baseline in CI: it loads, can be compared, and clears
the minimums in config/thresholds.yaml.

Step 11 (#16). The gate compares candidates against `evals/baseline.json`, so
a baseline that's malformed, records no tool data, didn't score every case, or
scores below the agreed floor would make every later comparison meaningless.
Until a baseline exists it only prints a notice. See docs/ci.md.
"""

import argparse
import sys
from pathlib import Path

import yaml
from pydantic import ValidationError

from recon.contracts import EvalRun
from recon.eval.gate import incomplete_run_failures
from recon.eval.harness import with_cost_per_correct_answer
from recon.eval.thresholds import DEFAULT_THRESHOLDS_PATH, load_thresholds

DEFAULT_BASELINE = Path("evals/baseline.json")


def baseline_problems(baseline: EvalRun, minimums: dict[str, float]) -> list[str]:
    """Why `baseline` can't serve as the gate's reference. Empty means it can."""
    problems = []
    if baseline.tool_data_snapshot is None:
        problems.append("it doesn't record which tool data it queried")
    problems.extend(incomplete_run_failures(baseline, "baseline"))
    for metric, floor in sorted(minimums.items()):
        value = baseline.aggregate.get(metric)
        if value is None:
            problems.append(f"{metric} is missing, minimum {floor}")
        elif value < floor:
            problems.append(f"{metric} is {value:.3f}, below the minimum {floor}")
    return problems


def main(argv: list[str] | None = None) -> int:
    """Exit 1 when the baseline is unusable, 0 when it passes or doesn't exist."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--thresholds", type=Path, default=DEFAULT_THRESHOLDS_PATH)
    args = parser.parse_args(argv)

    try:
        thresholds = load_thresholds(args.thresholds)
    except (FileNotFoundError, ValueError, yaml.YAMLError) as exc:
        print(f"{args.thresholds} can't be read: {exc}")
        return 1
    if not args.baseline.exists():
        print(f"{args.baseline} doesn't exist yet; nothing to check.")
        return 0
    try:
        baseline = EvalRun.model_validate_json(args.baseline.read_text("utf-8"))
    except ValidationError as exc:
        print(f"{args.baseline} isn't a valid EvalRun:\n{exc}")
        return 1
    problems = baseline_problems(baseline, thresholds.baseline_minimums)
    if problems:
        print(f"{args.baseline} can't serve as the baseline:")
        print("\n".join(f"- {problem}" for problem in problems))
        print("Regenerate it through an explicit PR (docs/contracts.md §9).")
        return 1
    run = with_cost_per_correct_answer(baseline, thresholds.correct_answer_score)
    checked = len(thresholds.baseline_minimums)
    print(f"{args.baseline} ({baseline.run_id}) passes; {checked} minimum(s) checked.")
    print(f"  total_cost_eur: {run.total_cost_eur:.4f}")
    for metric in ("answer_score_mean", "cost_per_correct_answer_eur"):
        if metric in run.aggregate:
            print(f"  {metric}: {run.aggregate[metric]:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
