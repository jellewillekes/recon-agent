"""Command-line entry point: `python -m recon.cli <command>`."""

import argparse
from collections import Counter
from pathlib import Path

from recon.adapters.finance_agent_bench import (
    DEFAULT_CACHE_FILENAME,
    fetch_csv,
    load_cases,
)
from recon.contracts import Case

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

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
