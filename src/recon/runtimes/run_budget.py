"""Which `config/models.yaml` budget a run gets: `run_budget:` for single
mode, with `run_budget_multi:`'s values over it for multi mode.

Multi mode runs a supervisor, workers and a critic per case, so it needs more
wall clock than single mode (averaged 109 s against 58 s on the text cases).
"""

from typing import Any


def budget_section(config: dict[str, Any], mode: str) -> dict[str, Any]:
    """The budget for `mode`: the defaults, plus multi mode's overrides."""
    section = dict(config["run_budget"])
    if mode == "multi":
        section.update(config.get("run_budget_multi") or {})
    return section
