"""Tests for `eval/judge.py`.

Monkeypatches `claude_agent_sdk.query` with a fabricated structured-output
message stream — no real model call, no network, no cost — same pattern
`tests/test_runtimes.py` uses for the investigator runtime.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage

from recon.contracts import AgentResult, Case
from recon.eval import judge
from recon.eval.rubrics import Assertion, Rubric

CASE_WITH_RUBRIC = Case(
    case_id="finance-agent-bench:1",
    source="finance-agent-bench",
    question="What was the revenue trend?",
    expected_answer="Revenue grew each quarter.",
    expected_tool_path=None,
    context={
        "question_type": "Trends",
        "expert_time_minutes": 12.0,
        "rubric": [
            {"operator": "correctness", "criteria": "Revenue grew each quarter"},
            {"operator": "contradiction", "criteria": "Revenue declined"},
        ],
    },
    tags=["Trends"],
    license="MIT",
    attribution="attribution",
)

CASE_NO_RUBRIC = Case(
    case_id="finance-agent-bench:2",
    source="finance-agent-bench",
    question="q",
    expected_answer="a",
    expected_tool_path=None,
    context={"rubric": []},
    tags=["Trends"],
    license="MIT",
    attribution="attribution",
)


def _agent_result(**overrides: object) -> AgentResult:
    defaults: dict[str, object] = {
        "case_id": "case-1",
        "answer": "Revenue grew steadily.",
        "evidence": ["ev"],
        "confidence": "high",
        "tool_calls": [],
        "runtime": "agent_sdk",
        "mode": "single",
        "tokens_in": 10,
        "tokens_out": 5,
        "cost_eur": 0.0,
        "elapsed_ms": 100,
        "error": None,
    }
    defaults.update(overrides)
    return AgentResult(**defaults)  # type: ignore[arg-type]


def _rubric(dimension: str, *assertions: Assertion, weight: float = 0.3) -> Rubric:
    return Rubric(
        dimension=dimension, version=1, weight=weight, assertions=list(assertions)
    )


@pytest.mark.unit
def test_build_judge_items_skips_answer_correctness_static_assertions() -> None:
    rubrics = {
        "answer_correctness": _rubric("answer_correctness"),
        "evidence_grounding": _rubric(
            "evidence_grounding", Assertion(id="a1", text="t1", score_if_true=1.0)
        ),
    }

    items = judge.build_judge_items(CASE_WITH_RUBRIC, rubrics)

    dimensions = {item.dimension for item in items}
    assert "evidence_grounding" in dimensions
    ids = {item.id for item in items}
    assert "evidence_grounding:a1" in ids
    # per-case criteria expanded under answer_correctness
    assert "answer_correctness:case_0" in ids
    assert "answer_correctness:case_1" in ids


@pytest.mark.unit
def test_per_case_items_map_operator_to_expect() -> None:
    items = judge._per_case_items(CASE_WITH_RUBRIC)

    by_id = {item.id: item for item in items}
    assert by_id["answer_correctness:case_0"].expect is True  # correctness
    assert by_id["answer_correctness:case_1"].expect is False  # contradiction


@pytest.mark.unit
def test_per_case_items_empty_when_no_rubric() -> None:
    assert judge._per_case_items(CASE_NO_RUBRIC) == []


@pytest.mark.unit
def test_score_dimensions_full_credit_when_all_expectations_met() -> None:
    items = judge._per_case_items(CASE_WITH_RUBRIC)
    holds = {"answer_correctness:case_0": True, "answer_correctness:case_1": False}

    scores = judge._score_dimensions(items, holds)

    assert scores["answer_correctness"] == 1.0


@pytest.mark.unit
def test_score_dimensions_partial_credit() -> None:
    items = judge._per_case_items(CASE_WITH_RUBRIC)
    # contradiction incorrectly holds true -> only 1 of 2 satisfied
    holds = {"answer_correctness:case_0": True, "answer_correctness:case_1": True}

    scores = judge._score_dimensions(items, holds)

    assert scores["answer_correctness"] == 0.5


@pytest.mark.unit
def test_score_dimensions_missing_id_defaults_to_false() -> None:
    items = judge._per_case_items(CASE_WITH_RUBRIC)
    scores = judge._score_dimensions(items, holds={})

    # case_0 expects True (missing -> False -> unsatisfied),
    # case_1 expects False (missing -> False -> satisfied)
    assert scores["answer_correctness"] == 0.5


def _patch_query(
    monkeypatch: pytest.MonkeyPatch, result_message: ResultMessage
) -> None:
    async def fake_query(
        *, prompt: str, options: ClaudeAgentOptions | None = None
    ) -> AsyncIterator[object]:
        yield result_message

    monkeypatch.setattr(judge, "query", fake_query)


def _result_message(**overrides: object) -> ResultMessage:
    defaults: dict[str, object] = {
        "subtype": "success",
        "duration_ms": 100,
        "duration_api_ms": 80,
        "is_error": False,
        "num_turns": 1,
        "session_id": "s1",
        "total_cost_usd": 0.001,
        "usage": {},
        "structured_output": {
            "results": [
                {"id": "evidence_grounding:a1", "holds": True},
                {"id": "answer_correctness:case_0", "holds": True},
                {"id": "answer_correctness:case_1", "holds": False},
            ]
        },
    }
    defaults.update(overrides)
    return ResultMessage(**defaults)  # type: ignore[arg-type]


@pytest.mark.unit
def test_judge_case_returns_scores_and_cost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_query(monkeypatch, _result_message())
    config_path = tmp_path / "models.yaml"
    config_path.write_text(
        "judge:\n  model: claude-sonnet-5\n  max_turns: 2\nusd_to_eur_rate: 0.9\n",
        encoding="utf-8",
    )
    rubrics = {
        "answer_correctness": _rubric("answer_correctness"),
        "evidence_grounding": _rubric(
            "evidence_grounding", Assertion(id="a1", text="t1", score_if_true=1.0)
        ),
        "tool_efficiency": _rubric("tool_efficiency"),  # no assertions
    }

    result = judge.judge_case(
        CASE_WITH_RUBRIC, _agent_result(), rubrics, models_config_path=config_path
    )

    assert result.rubric_scores["answer_correctness"] == 1.0
    assert result.rubric_scores["evidence_grounding"] == 1.0
    assert result.rubric_scores["tool_efficiency"] == 1.0  # vacuous, no assertions
    assert result.cost_eur == pytest.approx(0.001 * 0.9)


@pytest.mark.unit
def test_judge_case_skips_model_call_when_no_items(tmp_path: Path) -> None:
    rubrics = {"answer_correctness": _rubric("answer_correctness")}

    result = judge.judge_case(
        CASE_NO_RUBRIC,
        _agent_result(),
        rubrics,
        models_config_path=tmp_path / "unused.yaml",
    )

    assert result.rubric_scores == {"answer_correctness": 1.0}
    assert result.cost_eur == 0.0
