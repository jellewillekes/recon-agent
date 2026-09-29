#!/usr/bin/env python3
"""Stop hook: before Claude ends a turn, run `make test` if Python files changed.

A failing run blocks the stop once per prompt (exit 2) and shows Claude the
tail of the output. If the tests still fail at the next stop for the same
prompt, the hook lets Claude finish and adds a note it must pass on, rather
than looping. Adapted from the agentic-framework repo's `stop_gate.py`; see
docs/adr/0017-claude-code-project-setup.md.

Skipped in GitHub Actions (the review bots have no synced venv) and when
RECON_STOP_GATE=0 is set in Claude Code's environment.

Standard library only: hooks run under the system `python3`, not the venv.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Below the 300 s `timeout` this hook has in .claude/settings.json, so the
# hook reports a slow run itself instead of being killed.
TEST_TIMEOUT_S = 280
TAIL_LINES = 40
_BLOCKED_DIR = Path(tempfile.gettempdir()) / "recon-stop-gate"


def changed_python_files(cwd: str) -> list[str]:
    """Modified, staged and untracked `.py` files, one entry per file.

    `-uall` lists files inside new directories (plain `--porcelain` shows
    only `?? pkg/`), and `-z` keeps rename entries and odd paths parseable.
    """
    result = subprocess.run(
        ["git", "status", "--porcelain", "-z", "-uall"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    entries = result.stdout.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        if status[0] in "RC":
            i += 1  # a rename or copy is followed by its source path
        if path.endswith(".py") and "D" not in status:
            paths.append(path)
    return paths


def _already_blocked(event: dict[str, object]) -> bool:
    """Whether this hook already blocked a stop for the same prompt.

    Tracked in a marker file keyed on session and prompt, so a fresh prompt
    always gets one blocking check. Falls back to Claude Code's
    `stop_hook_active` flag when the event carries no `prompt_id`.
    """
    session, prompt = event.get("session_id"), event.get("prompt_id")
    if not session or not prompt:
        return event.get("stop_hook_active") is False
    key = hashlib.sha256(f"{session}:{prompt}".encode()).hexdigest()[:32]
    marker = _BLOCKED_DIR / key
    if marker.exists():
        return True
    _BLOCKED_DIR.mkdir(parents=True, exist_ok=True)
    marker.touch()
    return False


def _run_tests(cwd: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["make", "test"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=TEST_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        return True, ""
    except subprocess.TimeoutExpired:
        return False, f"`make test` took longer than {TEST_TIMEOUT_S}s."
    tail = "\n".join((result.stdout + result.stderr).splitlines()[-TAIL_LINES:])
    return result.returncode == 0, tail


def main() -> int:
    if os.environ.get("RECON_STOP_GATE") == "0" or os.environ.get("GITHUB_ACTIONS"):
        return 0
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        event = {}
    cwd = event.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    if not changed_python_files(cwd):
        return 0
    passed, output = _run_tests(cwd)
    if passed:
        return 0
    if _already_blocked(event):
        note = (
            "`make test` still fails. Do not claim the work is done: tell the "
            f"user which tests fail and why.\n\n{output}"
        )
        context = {"hookEventName": "Stop", "additionalContext": note}
        print(json.dumps({"hookSpecificOutput": context}))
        return 0
    print(
        "Stop gate: `make test` fails after your changes. Fix the cause before "
        "finishing; do not edit, skip or loosen tests to get past this. If you "
        f"believe a test is wrong, say so and stop.\n\n{output}",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
