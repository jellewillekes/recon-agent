#!/usr/bin/env python3
"""Fail when a prompt changed without `evals/baseline.json` being regenerated.

The baseline records a hash of every prompt it was scored with
(`EvalRun.prompt_hashes`). A prompt that no longer matches means the committed
baseline scores a different agent, so the promotion gate would compare against
the wrong thing. Runs as a pre-commit hook and in CI. Until a baseline exists
it only prints a notice. See docs/ci.md.
"""

import argparse
import sys
from pathlib import Path

import yaml

from recon.contracts import EvalRun
from recon.eval.hashing import DEFAULT_PROMPTS_DIR, compute_prompt_hashes, prompts_read

DEFAULT_BASELINE = Path("evals/baseline.json")
DEFAULT_ROLES_CONFIG = Path("config/roles.yaml")


def prompt_drift(recorded: dict[str, str], current: dict[str, str]) -> list[str]:
    """One line per role whose prompt differs from the baseline's hash."""
    drift = []
    for role in sorted(recorded.keys() | current.keys()):
        if role not in current:
            drift.append(f"prompts/{role}.md: removed since the baseline")
        elif role not in recorded:
            drift.append(f"prompts/{role}.md: added since the baseline")
        elif recorded[role] != current[role]:
            drift.append(f"prompts/{role}.md: changed since the baseline")
    return drift


def main(argv: list[str] | None = None) -> int:
    """Exit 1 on drift, 0 when prompts match or no baseline exists yet."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--prompts-dir", type=Path, default=DEFAULT_PROMPTS_DIR)
    parser.add_argument("--roles-config", type=Path, default=DEFAULT_ROLES_CONFIG)
    args = parser.parse_args(argv)

    if not args.baseline.exists():
        print(f"{args.baseline} doesn't exist yet; prompt hashes not checked.")
        return 0
    baseline = EvalRun.model_validate_json(args.baseline.read_text(encoding="utf-8"))
    roles = yaml.safe_load(args.roles_config.read_text(encoding="utf-8")) or {}
    pinned = prompts_read(baseline.runtime, baseline.mode, roles)
    recorded, current = baseline.prompt_hashes, compute_prompt_hashes(args.prompts_dir)
    unpinned = []
    if pinned is not None:
        # ADR 0032: only the prompts the baseline's run read are pinned.
        unpinned = prompt_drift(
            {r: h for r, h in recorded.items() if r not in pinned},
            {r: h for r, h in current.items() if r not in pinned},
        )
        recorded = {r: h for r, h in recorded.items() if r in pinned}
        current = {r: h for r, h in current.items() if r in pinned}
    if unpinned:
        print(
            f"Not read by the baseline's {baseline.runtime}/{baseline.mode} run, "
            "so not pinned:\n" + "\n".join(unpinned)
        )
    drift = prompt_drift(recorded, current)
    if not drift:
        return 0
    print("\n".join(drift))
    print(
        f"\n{args.baseline} was scored with different prompts. Run a full "
        "`recon.cli eval` with these prompts and replace the baseline in the "
        "same PR, or revert the prompt change. Replacing the baseline is the "
        "user's decision (AGENTS.md)."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
