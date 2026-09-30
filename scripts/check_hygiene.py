#!/usr/bin/env python3
"""Check that no company name reaches the repo, its commits or PR text.

AGENTS.md forbids naming companies in code, commits and docs. The denylist
would itself contain the names, so it's never committed. It comes from the
`HYGIENE_DENYLIST` environment variable (a CI secret) or the gitignored
`.hygiene-denylist` file: one name per line, `#` starts a comment. Names match
as whole words, ignoring case. Output gives locations only, never the name.

    check_hygiene.py check [--commits A..B] [--message-file F] [--require-list]
    check_hygiene.py propose TICKERS_FILE... [--out .hygiene-denylist]

`check` also scans `PR_TITLE` and `PR_BODY` when set, and a commit message
being written (`--message-file`, from the pre-commit `commit-msg` stage). `propose` drafts the
list from the reviewed tickers files `recon.cli edgar fetch` uses, for review
by hand. See docs/ci.md.
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

from recon.adapters.sec_edgar_tickers import _normalize_name

DENYLIST_ENV = "HYGIENE_DENYLIST"
DENYLIST_FILE = Path(".hygiene-denylist")
# Registered-name leftovers that prose doesn't use ("... & Co." -> "... AND").
_TRAILING_NOISE = frozenset({"AND", "COM"})


def parse_denylist(text: str) -> list[str]:
    """Names from the denylist text, skipping blank lines and comments."""
    lines = (line.split("#", 1)[0].strip() for line in text.splitlines())
    return sorted({line for line in lines if line})


def load_denylist() -> list[str]:
    """The environment variable wins over the local file. Empty if neither."""
    text = os.environ.get(DENYLIST_ENV, "")
    if not text.strip() and DENYLIST_FILE.exists():
        text = DENYLIST_FILE.read_text(encoding="utf-8")
    return parse_denylist(text)


def compile_denylist(names: list[str]) -> re.Pattern[str]:
    """Whole-word, case-insensitive. Words of a name may be joined by up to
    three separator characters, so "A-B", "A.B." and "A B" all match."""
    words = (re.findall(r"[A-Za-z0-9]+", name) for name in names)
    alternatives = [r"[^A-Za-z0-9\n]{1,3}".join(map(re.escape, w)) for w in words if w]
    return re.compile(
        rf"(?<![A-Za-z0-9])(?:{'|'.join(alternatives)})(?![A-Za-z0-9])", re.IGNORECASE
    )


def matching_lines(text: str, pattern: re.Pattern[str]) -> list[int]:
    """1-based numbers of the lines in `text` that contain a name."""
    return [n for n, line in enumerate(text.splitlines(), 1) if pattern.search(line)]


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout


def scan_tracked_files(pattern: re.Pattern[str]) -> list[str]:
    """`path:line` for every tracked text file line that contains a name."""
    hits = []
    for path in filter(None, _git("ls-files", "-z").split("\0")):
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue  # binary, or deleted/submodule in the working tree
        hits += [f"{path}:{n}" for n in matching_lines(text, pattern)]
    return hits


def scan_commits(rev_range: str, pattern: re.Pattern[str]) -> list[str]:
    """`commit <sha> message line N` for commit messages in `rev_range`."""
    hits = []
    log = _git("log", "--format=%H%x00%B%x1e", rev_range)
    for entry in filter(str.strip, log.split("\x1e")):
        sha, _, message = entry.strip().partition("\0")
        hits += [
            f"commit {sha[:12]} message line {n}"
            for n in matching_lines(message, pattern)
        ]
    return hits


def scan_pull_request(pattern: re.Pattern[str]) -> list[str]:
    """The PR title and body, passed in as `PR_TITLE` and `PR_BODY`."""
    hits = (
        ["PR title"] if matching_lines(os.environ.get("PR_TITLE", ""), pattern) else []
    )
    body = os.environ.get("PR_BODY", "")
    return hits + [f"PR body line {n}" for n in matching_lines(body, pattern)]


def propose(tickers_files: list[Path]) -> list[str]:
    """Short names from the registered-name column of reviewed tickers files."""
    names = set()
    for path in tickers_files:
        for line in path.read_text(encoding="utf-8").splitlines():
            columns = line.split("\t")
            if line.startswith("#") or len(columns) < 4:
                continue
            words = list(_normalize_name(columns[3]))
            while words and words[-1] in _TRAILING_NOISE:
                words.pop()
            if words:
                names.add(" ".join(words).title())
    return sorted(names)


def _check(args: argparse.Namespace) -> int:
    names = load_denylist()
    if not names:
        where = f"the {DENYLIST_ENV} secret or a local {DENYLIST_FILE}"
        if args.require_list:
            print(f"No denylist found. Set {where}; see docs/ci.md.")
            return 2
        print(f"No denylist found in {where}; company names not checked.")
        return 0
    pattern = compile_denylist(names)
    hits = scan_tracked_files(pattern) + scan_pull_request(pattern)
    if args.commits:
        hits += scan_commits(args.commits, pattern)
    if args.message_file:
        message = args.message_file.read_text(encoding="utf-8")
        hits += [f"commit message line {n}" for n in matching_lines(message, pattern)]
    if not hits:
        print(f"No company names found ({len(names)} names checked).")
        return 0
    print("\n".join(hits))
    print(
        f"\n{len(hits)} location(s) name a company from the denylist. Describe "
        "it by category and role instead (AGENTS.md). The name isn't printed, "
        f"so the log doesn't repeat it; check locally with {DENYLIST_FILE}."
    )
    return 1


def _propose(args: argparse.Namespace) -> int:
    if args.out.exists():
        print(f"{args.out} already exists and may hold edits. Delete it first.")
        return 1
    header = (
        "# Company names for scripts/check_hygiene.py, drafted from the tickers\n"
        "# files. Edit to the forms prose uses, then copy into the\n"
        f"# {DENYLIST_ENV} secret. Gitignored: never commit this file.\n"
    )
    args.out.write_text(header + "".join(f"{n}\n" for n in propose(args.tickers)))
    print(f"Wrote {args.out}. Review it before use.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run `check` or `propose`."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="scan files, commits and PR text")
    check.add_argument("--commits", help="also scan messages in this range, A..B")
    check.add_argument("--message-file", type=Path, help="a commit message to scan")
    check.add_argument("--require-list", action="store_true")
    check.set_defaults(run=_check)
    draft = commands.add_parser("propose", help="draft the denylist")
    draft.add_argument("tickers", nargs="+", type=Path)
    draft.add_argument("--out", type=Path, default=DENYLIST_FILE)
    draft.set_defaults(run=_propose)
    args = parser.parse_args(argv)
    result: int = args.run(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
