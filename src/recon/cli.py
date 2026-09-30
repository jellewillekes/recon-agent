"""Command-line entry point: `python -m recon.cli <command>`."""

import argparse
import json
from collections import Counter
from pathlib import Path

from recon.adapters import (
    sec_edgar,
    sec_edgar_normalize,
    sec_edgar_store,
    sec_edgar_tickers,
)
from recon.adapters.finance_agent_bench import (
    DEFAULT_CACHE_FILENAME,
    SOURCE,
    fetch_csv,
    load_cases,
)
from recon.contracts import Case, EvalRun
from recon.eval.gate import check_gate, comparability_failures
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
        dataset=SOURCE,
        tool_data_snapshot=tool_data_snapshot,
        case_ids=[case.case_id for case in cases],
        baseline=baseline,
    )
    if failures:
        for failure in failures:
            print(f"GATE REFUSED (before running): {failure}")
        raise SystemExit(1)


def _cmd_eval(args: argparse.Namespace) -> None:
    cases = load_cases(fetch_csv(args.path))
    if args.limit is not None:
        cases = cases[: args.limit]
    # Raises now, not mid-run, when the EDGAR cache is missing.
    tool_data_snapshot = tool_data_snapshot_id()
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

    if baseline is not None:
        failures = check_gate(run, baseline)
        if failures:
            for failure in failures:
                print(f"GATE FAILED: {failure}")
            raise SystemExit(1)
        print("Gate passed.")


def _derive_tickers_file(
    edgar: sec_edgar.EdgarClient, dataset_path: Path, tickers_path: Path
) -> None:
    company_tickers = sec_edgar.fetch_company_tickers(edgar, sec_edgar.DEFAULT_RAW_DIR)
    cases = load_cases(fetch_csv(dataset_path))
    matches = sec_edgar_tickers.derive_tickers(
        cases, sec_edgar_tickers.load_company_tickers(company_tickers)
    )
    unmatched = sec_edgar_tickers.unmatched_case_ids(cases, matches)
    sec_edgar_tickers.write_tickers_file(tickers_path, matches, unmatched)
    print(
        f"Matched {len(matches)} companies; {len(unmatched)} of {len(cases)} cases "
        f"had no match. Review {tickers_path}, then run `recon.cli edgar fetch`."
    )


def _tickers_paths(args: argparse.Namespace) -> list[Path]:
    paths: list[Path] | None = args.tickers_file
    return paths or [sec_edgar.DEFAULT_RAW_DIR / sec_edgar.TICKERS_FILENAME]


def _cmd_edgar_fetch(args: argparse.Namespace) -> None:
    config = sec_edgar.load_config(args.config)
    raw_dir = sec_edgar.DEFAULT_RAW_DIR
    tickers_paths = _tickers_paths(args)
    with sec_edgar.build_client(sec_edgar.user_agent_from_env()) as http:
        edgar = sec_edgar.EdgarClient(http, config.max_requests_per_second)
        if args.from_dataset:
            if len(tickers_paths) != 1:
                raise SystemExit("--from-dataset writes exactly one --tickers-file.")
            _derive_tickers_file(edgar, args.path, tickers_paths[0])
            return
        tickers = sec_edgar_tickers.read_tickers_files(tickers_paths)
        snapshot = sec_edgar.snapshot_id()
        snapshot_dir = raw_dir / snapshot
        fetched: dict[str, dict[str, object]] = {}
        for i, (ticker, cik) in enumerate(sorted(tickers.items()), 1):
            print(f"[{i}/{len(tickers)}] {ticker} (CIK {cik})")
            fetched[ticker] = {
                "cik": cik,
                **sec_edgar.fetch_company(edgar, cik, snapshot_dir),
            }
    sec_edgar.write_manifest(snapshot_dir, snapshot, config, fetched)
    out_dir = sec_edgar.DEFAULT_PROCESSED_DIR / snapshot
    counts = sec_edgar_normalize.normalize_snapshot(
        snapshot_dir, tickers, config, out_dir
    )
    print(f"Snapshot {snapshot} (cutoff {config.filed_cutoff}) -> {out_dir}: {counts}")


def _cmd_edgar_stats(args: argparse.Namespace) -> None:
    snapshot_dir = sec_edgar_store.latest_snapshot(sec_edgar.DEFAULT_PROCESSED_DIR)
    manifest = json.loads((snapshot_dir / sec_edgar.MANIFEST_FILENAME).read_text())
    print(f"snapshot: {snapshot_dir.name}  cutoff: {manifest['filed_cutoff']}")
    print(f"rows: {manifest['row_counts']}")
    print(f"{'company':<8} {'facts':>8} {'filings':>8}")
    for company_id, facts, filings in sec_edgar_store.company_stats(snapshot_dir):
        flag = "  <- no XBRL facts" if facts == 0 else ""
        print(f"{company_id:<8} {facts:>8} {filings:>8}{flag}")
    for tickers_path in _tickers_paths(args):
        if not tickers_path.exists():
            continue
        unmatched = [
            line.removeprefix("# unmatched: ")
            for line in tickers_path.read_text(encoding="utf-8").splitlines()
            if line.startswith("# unmatched: ")
        ]
        print(f"{tickers_path}: {len(unmatched)} cases with no matched company")
        for case_id in unmatched:
            print(f"  {case_id}")


def _add_edgar_parser(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> None:
    edgar_parser = subparsers.add_parser(
        "edgar", help="Fetch and inspect SEC EDGAR tool data (issue #58)."
    )
    edgar_sub = edgar_parser.add_subparsers(dest="edgar_command", required=True)

    fetch_parser = edgar_sub.add_parser(
        "fetch",
        help="Fetch the companies in the reviewed tickers file and build the "
        "Parquet tables. Needs SEC_EDGAR_USER_AGENT.",
    )
    fetch_parser.add_argument(
        "--from-dataset",
        action="store_true",
        help="Derive the tickers file from the dataset's questions for review, "
        "then stop. Nothing else is fetched.",
    )
    fetch_parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_DATASET_PATH,
        help="Local cache path for the dataset CSV (used with --from-dataset).",
    )
    fetch_parser.add_argument(
        "--config", type=Path, default=sec_edgar.DEFAULT_CONFIG_PATH
    )
    stats_parser = edgar_sub.add_parser(
        "stats", help="Row counts per company in the latest snapshot."
    )
    for sub in (fetch_parser, stats_parser):
        sub.add_argument(
            "--tickers-file",
            type=Path,
            action="append",
            help="Tickers file to read (repeatable; companies are merged) or, "
            "with --from-dataset, to write. Default: "
            f"{sec_edgar.DEFAULT_RAW_DIR / sec_edgar.TICKERS_FILENAME}.",
        )
    fetch_parser.set_defaults(func=_cmd_edgar_fetch)
    stats_parser.set_defaults(func=_cmd_edgar_stats)


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

    _add_edgar_parser(subparsers)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
