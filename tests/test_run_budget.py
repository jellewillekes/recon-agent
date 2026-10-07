"""Per-mode run budgets: multi mode runs a supervisor, workers and a critic
per case, and averaged 109 s on the text cases against single's 58 s."""

from pathlib import Path

import pytest
import yaml

from recon.runtimes.run_budget import budget_section

pytestmark = pytest.mark.unit

CONFIG = {
    "run_budget": {"max_tool_calls": 30, "max_tokens": 450000, "max_wall_clock_s": 150},
    "run_budget_multi": {"max_wall_clock_s": 240},
}


def test_single_mode_uses_the_default_budget() -> None:
    assert budget_section(CONFIG, "single")["max_wall_clock_s"] == 150


def test_multi_mode_overrides_only_what_it_names() -> None:
    multi = budget_section(CONFIG, "multi")
    assert multi["max_wall_clock_s"] == 240
    assert multi["max_tokens"] == 450000


def test_without_an_override_multi_mode_uses_the_default() -> None:
    assert (
        budget_section({"run_budget": CONFIG["run_budget"]}, "multi")
        == (CONFIG["run_budget"])
    )


def test_the_repo_budgets() -> None:
    """The values the user set on 2026-10-07."""
    config = yaml.safe_load(Path("config/models.yaml").read_text())
    assert budget_section(config, "single")["max_tokens"] == 450000
    assert budget_section(config, "single")["max_wall_clock_s"] == 150
    assert budget_section(config, "multi")["max_wall_clock_s"] == 240
