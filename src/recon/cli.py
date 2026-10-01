"""Command-line entry point: `python -m recon.cli <command>`."""

import argparse
from collections import Counter
from pathlib import Path

from recon import cli_edgar
from recon.adapters import sec_edgar, sec_edgar_tickers
from recon.adapters.finance_agent_bench import (
    DATASET_ID,
    DEFAULT_CACHE_FILENAME,
    fetch_csv,
    load_cases,
)
from recon.contracts import Case, EvalRun
from recon.eval import case_selection
from recon.eval.gate import SKIPPED_AT_COST_CAP, check_gate, comparability_failures
from recon.eval.harness import RUBRIC_VERSION, run_evaluation
from recon.eval.report import render_markdown
from recon.runtimes.agent_sdk import AgentSdkRuntime
from recon.runtimes.base import Runtime
from recon.runtimes.langgraph import LangGraphRuntime
from recon.tools.data_source import tool_data_snapshot_id

# Filename carries the pinned commit, so bumping the pin in the adapter also
# changes the default fetch destination here — an old pin's cached file is
# never mistaken for the current one.
DEFAULT_DATASET_PATH = Path("data/raw/finance_agent_bench") / DEFAULT_CACHE_FILENAME

DEFAULT_MAX_COST_EUR = 1.0
# Per case, agent plus judge, for the pre-run estimate only. From #67's
# isolated measurement (3 cases, €0.128). Replace it with the agent_cost_eur
# and judge_cost_eur of a real run once one exists.
ESTIMATED_COST_EUR_PER_CASE = 0.05


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
        return LangGraphRuntime(mode=mode)  # type: ignore[arg-type]
    return AgentSdkRuntime(mode=mode)  # type: ignore[arg-type]


def _cmd_run(args: argparse.Namespace) -> None:
    csv_path = fetch_csv(args.path)
    case = _find_case(load_cases(csv_path), args.case_id)
    result = _build_runtime(args.runtime, args.mode).run(case)
    print(result.model_dump_json(indent=2))


def _refuse_incomparable_baseline(
    baseline: EvalRun, cases: list[Case], tool_data_snapshot: str
) -> None:
    """Stop before any credit is spent if the gate would refuse the comparison."""
    failures = comparability_failures(
        rubric_version=RUBRIC_VERSION,
        dataset=DATASET_ID,
        tool_data_snapshot=tool_data_snapshot,
        case_ids=[case.case_id for case in cases],
        baseline=baseline,
    )
    if failures:
        for failure in failures:
            print(f"GATE REFUSED (before running): {failure}")
        raise SystemExit(1)


def _select_cases(args: argparse.Namespace) -> list[Case]:
    cases = load_cases(fetch_csv(args.path))
    try:
        if args.cases is not None:
            ids = case_selection.read_case_file(args.cases)
            cases = case_selection.select_cases(cases, ids)
        elif args.company is not None:
            by_ticker = sec_edgar_tickers.read_case_ids(_case_tickers_paths(args))
            ids = by_ticker.get(args.company.upper(), [])
            if not ids:
                raise ValueError(
                    f"No case ids recorded for {args.company} in the tickers files. "
                    "Only files written by `edgar fetch --from-dataset` carry them."
                )
            cases = case_selection.select_cases(cases, ids)
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    return cases if args.limit is None else cases[: args.limit]


def _case_tickers_paths(args: argparse.Namespace) -> list[Path]:
    paths: list[Path] | None = args.tickers_file
    return paths or sorted(sec_edgar.DEFAULT_RAW_DIR.glob("tickers*.txt"))


def _refuse_over_cap(case_count: int, max_cost_eur: float) -> None:
    """Stop before any credit is spent if the estimate already exceeds the cap."""
    estimate = case_count * ESTIMATED_COST_EUR_PER_CASE
    print(f"{case_count} case(s), estimated €{estimate:.2f}, cap €{max_cost_eur:.2f}")
    if estimate > max_cost_eur:
        raise SystemExit(
            "The estimate exceeds the cap. Run fewer cases (--cases, --company, "
            "--limit) or raise --max-cost-eur."
        )


def _cmd_eval(args: argparse.Namespace) -> None:
    cases = _select_cases(args)
    _refuse_over_cap(len(cases), args.max_cost_eur)
    # Fails now, not mid-run, when the EDGAR cache is missing.
    try:
        tool_data_snapshot = tool_data_snapshot_id()
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    baseline = (
        EvalRun.model_validate_json(args.baseline.read_text(encoding="utf-8"))
        if args.baseline is not None
        else None
    )
    if baseline is not None:
        _refuse_incomparable_baseline(baseline, cases, tool_data_snapshot)
    run = run_evaluation(
        cases,
        _build_runtime(args.runtime, args.mode),
        tool_data_snapshot=tool_data_snapshot,
        max_cost_eur=args.max_cost_eur,
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
    if run.aggregate.get(SKIPPED_AT_COST_CAP):
        raise SystemExit(
            f"Stopped at the €{args.max_cost_eur:.2f} cap before "
            f"{run.aggregate[SKIPPED_AT_COST_CAP]:.0f} case(s). The gate refuses "
            "this run; rerun with fewer cases or a higher --max-cost-eur."
        )

    if baseline is not None:
        failures = check_gate(run, baseline)
        if failures:
            for failure in failures:
                print(f"GATE FAILED: {failure}")
            raise SystemExit(1)
        print("Gate passed.")


def _cmd_cases(args: argparse.Namespace) -> None:
    by_ticker = sec_edgar_tickers.read_case_ids(_case_tickers_paths(args))
    cases = load_cases(fetch_csv(args.path))
    try:
        picked = case_selection.spread_across_companies(cases, by_ticker, args.spread)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print("\n".join(picked))


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
    subset = eval_parser.add_mutually_exclusive_group()
    subset.add_argument(
        "--cases",
        type=Path,
        help="File of case ids to run, e.g. evals/smoke-cases.txt.",
    )
    subset.add_argument("--company", help="Run only the cases about this ticker.")
    eval_parser.add_argument(
        "--tickers-file",
        type=Path,
        action="append",
        help="Tickers files holding case ids, for --company (repeatable). Default: "
        f"every tickers*.txt in {sec_edgar.DEFAULT_RAW_DIR}.",
    )
    eval_parser.add_argument(
        "--max-cost-eur",
        type=float,
        default=DEFAULT_MAX_COST_EUR,
        help="Refuse to start above this estimate, and stop before a case that "
        f"could pass it (default €{DEFAULT_MAX_COST_EUR:.2f}).",
    )
    eval_parser.set_defaults(func=_cmd_eval)

    cases_parser = subparsers.add_parser(
        "cases", help="Print case ids spread across companies, for --cases."
    )
    cases_parser.add_argument("--spread", type=int, required=True)
    cases_parser.add_argument("--path", type=Path, default=DEFAULT_DATASET_PATH)
    cases_parser.add_argument("--tickers-file", type=Path, action="append")
    cases_parser.set_defaults(func=_cmd_cases)

    cli_edgar.add_edgar_parser(subparsers, DEFAULT_DATASET_PATH)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
