"""Per-mode run budgets: multi mode runs a supervisor, workers and a critic
per case, and averaged 109 s on the text cases against single's 58 s."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from recon.runtimes.run_budget import budget_section

pytestmark = pytest.mark.unit


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


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
    """The values the user set on 2026-10-07 (multi 240 to 300 s for #119)."""
    config = yaml.safe_load(Path("config/models.yaml").read_text())
    assert budget_section(config, "single")["max_tokens"] == 450000
    assert budget_section(config, "single")["max_wall_clock_s"] == 150
    assert budget_section(config, "multi")["max_wall_clock_s"] == 300


@pytest.mark.anyio
async def test_the_sdk_multi_runtime_runs_on_the_multi_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from recon.contracts import Case
    from recon.runtimes import multi_agent

    budgets: list[float] = []

    class _Stop(Exception):
        pass

    def tracker(budget: Any) -> Any:
        budgets.append(budget.max_wall_clock_s)
        raise _Stop

    monkeypatch.setattr(multi_agent, "_load_roles_config", lambda path: {})
    monkeypatch.setattr(
        multi_agent,
        "_load_model_config",
        lambda path: CONFIG | {"usd_to_eur_rate": 0.9},
    )
    monkeypatch.setattr(multi_agent, "_BudgetTracker", tracker)
    case = Case(
        case_id="b-1",
        source="test",
        question="q",
        expected_answer="a",
        expected_tool_path=None,
        context={},
        tags=[],
        license="MIT",
        attribution="t",
    )
    with pytest.raises(_Stop):
        await multi_agent.run_multi_async(
            case, roles_config_path=Path("unused"), prompts_dir=Path("prompts")
        )
    assert budgets == [240]


@pytest.mark.anyio
async def test_the_langgraph_multi_runtime_runs_on_the_multi_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from recon.contracts import Case
    from recon.runtimes import langgraph as lg
    from recon.runtimes import langgraph_multi

    seen: dict[str, float] = {}

    async def run_multi_async(case: Any, **kwargs: Any) -> Any:
        seen["wall_clock"] = kwargs["max_wall_clock_s"]
        raise RuntimeError("stop after reading the budget")

    config = CONFIG | {"investigator": {"model": "m", "max_turns": 20}}
    monkeypatch.setattr(lg, "_load_model_config", lambda path: config)
    monkeypatch.setattr(langgraph_multi, "run_multi_async", run_multi_async)
    case = Case(
        case_id="b-2",
        source="test",
        question="q",
        expected_answer="a",
        expected_tool_path=None,
        context={},
        tags=[],
        license="MIT",
        attribution="t",
    )
    await lg.LangGraphRuntime(mode="multi").run_async(case)
    assert seen["wall_clock"] == 240
