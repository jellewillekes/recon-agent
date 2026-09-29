#!/usr/bin/env python3
"""PostToolUse hook: `ruff format` a Python file Claude just wrote or edited.

Format only, no `ruff check --fix`: that fix deletes an unused import, and
Claude often adds an import in one edit and its first use in the next. Lint
stays with `make check` and the Stop gate. Never blocks. Adapted from the
agentic-framework repo's `format_python.py`, which looks for `ruff` on PATH;
here ruff lives in the project venv, so this goes through `uv run`.

Standard library only: hooks run under the system `python3`, not the venv.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

TIMEOUT_S = 30


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    path = (event.get("tool_input") or {}).get("file_path") or ""
    uv = shutil.which("uv")
    if not path.endswith((".py", ".pyi")) or uv is None:
        return 0
    cwd = event.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    try:
        subprocess.run(
            [uv, "run", "--quiet", "ruff", "format", "--quiet", path],
            cwd=cwd,
            capture_output=True,
            timeout=TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 0  # formatting is a convenience; make check still catches it
    return 0


if __name__ == "__main__":
    sys.exit(main())
