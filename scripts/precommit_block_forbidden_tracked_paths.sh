#!/usr/bin/env bash
set -euo pipefail

forbidden_regex='(^\.venv/|^venv/|^\.pytest_cache/|^\.mypy_cache/|^\.ruff_cache/|^__pycache__/|\.DS_Store$)'

if git ls-files | grep -En "${forbidden_regex}"; then
  echo ""
  echo "ERROR: Forbidden files are tracked by git."
  echo "Remove them from the index and add them to .gitignore."
  exit 1
fi

stray_data_files=$(git ls-files -- 'data/' | grep -v '^data/README\.md$' || true)
if [[ -n "${stray_data_files}" ]]; then
  echo ""
  echo "ERROR: Files under data/ are tracked besides data/README.md:"
  echo "${stray_data_files}"
  echo "data/ is gitignored except its README (see CLAUDE.md)."
  exit 1
fi
