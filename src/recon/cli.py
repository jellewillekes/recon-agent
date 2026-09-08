"""Command-line entry point: `python -m recon.cli <command>`."""

import argparse
from collections import Counter
from pathlib import Path

from recon.adapters.finance_agent_bench import (
    DEFAULT_CACHE_FILENAME,
    fetch_csv,
    load_cases,
)
from recon.contracts import Case, EvalRun
from recon.eval.gate import check_gate
from recon.eval.harness import run_evaluation
from recon.eval.report import render_markdown
from recon.runtimes.agent_sdk import AgentSdkRuntime
from recon.runtimes.base import Runtime
from recon.runtimes.langgraph import LangGraphRuntime

# Filename carries the pinned commit, so bumping the pin in the adapter also
# changes the default fetch destination here — an old pin's cached file is
# never mistaken for the current one.
DEFAULT_DATASET_PATH = Path("data/raw/finance_agent_bench") / DEFAULT_CACHE_FILENAME


def compute_dataset_stats(cases: list[Case]) -> dict[str, object]:
    """Pure summary of a loaded dataset — no I/O, so it's directly testable."""
    tag_counts = Counter(tag for case in cases for tag in case.tags)
    with_tool_path = sum(1 for case in cases if case.expected_tool_path is not None)
    return {
        "case_count": len(cases),
        "tag_counts": dict(tag_counts),
        "cases_with_expected_tool_path": with_tool_path,
    }


def _print_dataset_stats(stats: dict[str, object]) -> None:
    print(f"cases: {stats['case_count']}")
    print(f"cases with expected_tool_path: {stats['cases_with_expected_tool_path']}")
    print("tags:")
    tag_counts: dict[str, int] = stats["tag_counts"]  # type: ignore[assignment]
    for tag, count in sorted(tag_counts.items(), key=lambda item: -item[1]):
        print(f"  {count:>3}  {tag}")


def _cmd_dataset(args: argparse.Namespace) -> None:
    csv_path = fetch_csv(args.path)
    cases = load_cases(csv_path)
    if args.stats:
        _print_dataset_stats(compute_dataset_stats(cases))
    else:
        print(f"Loaded {len(cases)} cases from {csv_path}")


def _find_case(cases: list[Case], case_id: str) -> Case:
    for case in cases:
        if case.case_id == case_id:
            return case
    raise SystemExit(
        f"No case with case_id={case_id!r} in the dataset loaded from --path."
    )


def _build_runtime(runtime: str, mode: str) -> Runtime:
    if runtime == "langgraph":
        try:
            return LangGraphRuntime(mode=mode)  # type: ignore[arg-type]
        except NotImplementedError as exc:
            # LangGraphRuntime(mode="multi") raises rather than accept a mode
            # it can't run (issue #14 part 2 not landed yet) - a clean CLI
            # exit here beats letting that traceback surface raw.
            raise SystemExit(str(exc)) from exc
    return AgentSdkRuntime(mode=mode)  # type: ignore[arg-type]


def _cmd_run(args: argparse.Namespace) -> None:
    csv_path = fetch_csv(args.path)
    case = _find_case(load_cases(csv_path), args.case_id)
    result = _build_runtime(args.runtime, args.mode).run(case)
    print(result.model_dump_json(indent=2))


def _cmd_eval(args: argparse.Namespace) -> None:
    csv_path = fetch_csv(args.path)
    cases = load_cases(csv_path)
    run = run_evaluation(
        cases, _build_runtime(args.runtime, args.mode), limit=args.limit
    )

    results_dir = Path("evals/results")
    results_dir.mkdir(parents=True, exist_ok=True)
    json_path = results_dir / f"{run.run_id}.json"
    md_path = results_dir / f"{run.run_id}.md"
    json_path.write_text(run.model_dump_json(indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(run), encoding="utf-8")

    print(f"Wrote {json_path} and {md_path}")
    print(
        f"cases={len(run.case_scores)} "
        f"task_completion_rate={run.aggregate.get('task_completion_rate', 0.0):.3f} "
        f"answer_score_mean={run.aggregate.get('answer_score_mean', 0.0):.3f} "
        f"total_cost_eur={run.total_cost_eur:.4f}"
    )

    if args.baseline is not None:
        baseline = EvalRun.model_validate_json(
            args.baseline.read_text(encoding="utf-8")
        )
        failures = check_gate(run, baseline)
        if failures:
            for failure in failures:
                print(f"GATE FAILED: {failure}")
            raise SystemExit(1)
        print("Gate passed.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="recon")
    subparsers = parser.add_subparsers(dest="command", required=True)

    dataset_parser = subparsers.add_parser(
        "dataset", help="Inspect the loaded dataset."
    )
    dataset_parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_DATASET_PATH,
        help="Local cache path for the source CSV (fetched here if missing).",
    )
    dataset_parser.add_argument(
        "--stats", action="store_true", help="Print case count and tag distribution."
    )
    dataset_parser.set_defaults(func=_cmd_dataset)

    run_parser = subparsers.add_parser("run", help="Run one case through a runtime.")
    run_parser.add_argument(
        "--case-id", required=True, help="case_id of the case to run."
    )
    run_parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_DATASET_PATH,
        help="Local cache path for the source CSV (fetched here if missing).",
    )
    run_parser.add_argument(
        "--mode",
        choices=["single", "multi"],
        default="single",
        help="single: one investigator. multi: supervisor + workers + critic.",
    )
    run_parser.add_argument(
        "--runtime",
        choices=["sdk", "langgraph"],
        default="sdk",
        help="sdk: Claude Agent SDK, subscription credit. langgraph: LangGraph, "
        "needs ANTHROPIC_API_KEY (docs/adr/0010-langgraph-runtime.md).",
    )
    run_parser.set_defaults(func=_cmd_run)

    eval_parser = subparsers.add_parser("eval", help="Run the evaluation harness.")
    eval_parser.add_argument(
        "--limit", type=int, default=None, help="Only evaluate the first N cases."
    )
    eval_parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_DATASET_PATH,
        help="Local cache path for the source CSV (fetched here if missing).",
    )
    eval_parser.add_argument(
        "--mode",
        choices=["single", "multi"],
        default="single",
        help="single: one investigator. multi: supervisor + workers + critic.",
    )
    eval_parser.add_argument(
        "--runtime",
        choices=["sdk", "langgraph"],
        default="sdk",
        help="sdk: Claude Agent SDK, subscription credit. langgraph: LangGraph, "
        "needs ANTHROPIC_API_KEY (docs/adr/0010-langgraph-runtime.md).",
    )
    eval_parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="Baseline EvalRun JSON to gate against (docs/contracts.md §9). "
        "Exits non-zero on regression.",
    )
    eval_parser.set_defaults(func=_cmd_eval)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
