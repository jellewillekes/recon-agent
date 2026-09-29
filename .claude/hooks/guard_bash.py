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
import subprocess
import sys

OVERRIDE_ENV = "RECON_ALLOW_GUARDED"

# Segments are checked one at a time, so `make check && recon.cli eval`
# can't hide the second command behind the first.
_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;&|\n]")

_EVAL = re.compile(r"\brecon\.cli\s+eval\b")
_LIMIT = re.compile(r"--limit(=|\s+)\d+")
# `git push`, also with global options first: `git -c key=value push`.
_GIT_PUSH = re.compile(r"\bgit(\s+(-c\s+('[^']*'|\"[^\"]*\"|\S+)|-C\s+\S+))*\s+push\b")
_PUSH_TO_MAIN = re.compile(r"(\s|:|\+|refs/heads/)(main|master)(?![\w./-])")
_FORCE = re.compile(r"\s(--force(?![-\w])|-f\b|-\w*f\w*\b)")
_NO_VERIFY = re.compile(r"\bgit\b.*\s--no-verify\b")
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


def _check_push(args: str, cwd: str) -> str | None:
    """`args` is everything after `push` in one command segment."""
    if _FORCE.search(args):
        return "force-push rewrites shared history; use --force-with-lease"
    if _PUSH_TO_MAIN.search(args):
        return "pushing to main skips review; push a branch and open a PR"
    refspecs = [a for a in args.split() if not a.startswith("-")]
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
    push = _GIT_PUSH.search(segment)
    if push:
        reason = _check_push(segment[push.end() :], cwd)
        if reason:
            return reason
    if _NO_VERIFY.search(segment):
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
