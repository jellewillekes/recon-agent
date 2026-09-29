#!/usr/bin/env python3
"""PreToolUse hook: block Bash commands that cost credit or are hard to undo.

Claude Code sends the tool call as JSON on stdin. Exit code 2 blocks the call
and shows stderr to Claude; exit 0 hands the call to the normal permission
flow (`.claude/settings.json`). Adapted from the agentic-framework repo's
`guard_bash.py`; see docs/adr/0017-claude-code-project-setup.md.

Permission rules match the command text Claude usually writes and aren't a
security boundary. This hook is the second layer for variants they miss:
compound commands, refspecs like `HEAD:main`, and `cat` on a `.env` file.

To run a blocked command on purpose, run it yourself, or start Claude Code
with RECON_ALLOW_GUARDED=1 in its environment. Setting the variable inside
the command text does not count.

Standard library only: hooks run under the system `python3`, not the venv.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys

OVERRIDE_ENV = "RECON_ALLOW_GUARDED"

# Segments are checked one at a time, so `make check && recon.cli eval`
# can't hide the second command behind the first.
_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;&|\n]")

_EVAL = re.compile(r"\brecon\.cli\s+eval\b")
_LIMIT = re.compile(r"--limit(=|\s+)\d+")
# A push destination that is the default branch: `main`, `+main`,
# `HEAD:main`, `refs/heads/main`. Anchored, so `feat/main-menu` isn't one.
_MAIN_REF = re.compile(r"^\+?(?:[^:]*:)?(?:refs/heads/)?(?:main|master)$")
# git's global options that take a separate value: `git -c key=value push`.
_GIT_VALUE_OPTIONS = frozenset({"-c", "-C", "--git-dir", "--work-tree", "--namespace"})
_DESTRUCTIVE_SQL = re.compile(
    r"\b(drop\s+(table|schema|database)|truncate\s+table)\b", re.IGNORECASE
)
_READERS = frozenset(
    {"cat", "less", "more", "head", "tail", "bat", "grep", "rg", "awk", "sed"}
    | {"strings", "xxd", "od", "base64", "source", "."}
)
# `.env` or `docker/.env` as a path, but not `.env.example`.
_ENV_FILE = re.compile(r"(?<![\w.-])\.env(?![\w.-])")


def _current_branch(cwd: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _tokens(segment: str) -> list[str]:
    """Shell words of one segment. Unbalanced quotes fall back to whitespace."""
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _git_subcommand(words: list[str]) -> tuple[str, list[str]] | None:
    """`(subcommand, its args)` if `words` run git, skipping global options.

    Parsed as tokens rather than a regex: quoted `-c` values made an
    equivalent regex backtrack exponentially (a CodeQL finding).
    """
    if "git" not in words:
        return None
    i = words.index("git") + 1
    while i < len(words) and words[i].startswith("-"):
        i += 2 if words[i] in _GIT_VALUE_OPTIONS else 1
    if i >= len(words):
        return None
    return words[i], words[i + 1 :]


def _is_force(arg: str) -> bool:
    if arg == "--force":
        return True
    # Short flags only (`-f`, `-uf`); `--force-with-lease` is the safe form.
    return arg.startswith("-") and not arg.startswith("--") and "f" in arg[1:]


def _check_push(args: list[str], cwd: str) -> str | None:
    """`args` are the words after `git push` in one command segment."""
    if any(_is_force(a) for a in args):
        return "force-push rewrites shared history; use --force-with-lease"
    refspecs = [a for a in args if not a.startswith("-")]
    if any(_MAIN_REF.match(r) for r in refspecs[1:]):
        return "pushing to main skips review; push a branch and open a PR"
    if len(refspecs) <= 1 and _current_branch(cwd) in ("main", "master"):
        return "you are on main, so this push would update main directly"
    return None


def _check_segment(segment: str, cwd: str) -> str | None:
    if _EVAL.search(segment) and not _LIMIT.search(segment):
        return (
            "recon.cli eval without --limit runs every case and spends Agent SDK "
            "credit (AGENTS.md, Cost). Use --limit 3, or ask the user to run "
            "the full evaluation"
        )
    git = _git_subcommand(_tokens(segment))
    if git and git[0] == "push":
        reason = _check_push(git[1], cwd)
        if reason:
            return reason
    if git and "--no-verify" in git[1]:
        return "--no-verify skips the pre-commit checks; fix what they report"
    if _DESTRUCTIVE_SQL.search(segment):
        return "DROP and TRUNCATE delete data"
    words = segment.split()
    if words and words[0] in _READERS and _ENV_FILE.search(segment):
        return ".env files hold secrets; read .env.example instead"
    return None


def check(command: str, cwd: str) -> str | None:
    """The reason to block `command`, or None to let it through."""
    for segment in _SEGMENT_SPLIT.split(command):
        reason = _check_segment(segment.strip(), cwd)
        if reason:
            return reason
    return None


def main() -> int:
    if os.environ.get(OVERRIDE_ENV) == "1":
        return 0
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    command = (event.get("tool_input") or {}).get("command") or ""
    cwd = event.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    reason = check(command, cwd)
    if reason is None:
        return 0
    print(
        f"Blocked by .claude/hooks/guard_bash.py: {reason}.\n"
        "Do not retry this in another form. Tell the user what you wanted to "
        "run and why, and let them run it themselves.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
