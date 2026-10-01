"""`recon.cli edgar ...`: fetch and inspect the SEC EDGAR tool data (#58).

Split from `recon.cli` to keep each module under the size limit (AGENTS.md).
"""

import argparse
import json
from pathlib import Path

from recon.adapters import (
    sec_edgar,
    sec_edgar_normalize,
    sec_edgar_store,
    sec_edgar_tickers,
)
from recon.adapters.finance_agent_bench import fetch_csv, load_cases


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


def add_edgar_parser(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
    dataset_path: Path,
) -> None:
    """Register `edgar fetch` and `edgar stats` on the top-level parser."""
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
        default=dataset_path,
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
