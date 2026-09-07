"""Tests for `eval/harness.py`.

Uses a fake `Runtime` (implements both `.run(case)` and `.run_async(case)` per
`runtimes/base.py`; the harness only calls `.run`) and a monkeypatched
`judge_case` so no real model call happens here — the judge itself is
covered separately in `tests/test_eval_judge.py`.
"""

from pathlib import Path

import pytest

from recon.contracts import AgentResult, Case
from recon.eval import harness
from recon.eval.judge import JudgeResult
from recon.eval.rubrics import Rubric

REPO_RUBRICS_DIR = Path(__file__).parent.parent / "config" / "rubrics"
REPO_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
REPO_MODELS_CONFIG = Path(__file__).parent.parent / "config" / "models.yaml"


def _case(case_id: str, expected_tool_path: list[str] | None = None) -> Case:
    return Case(
        case_id=case_id,
        source="finance-agent-bench",
        question="q",
        expected_answer="a",
        expected_tool_path=expected_tool_path,
        context={"rubric": []},
        tags=["Trends"],
        license="MIT",
        attribution="attribution",
    )


class _FakeRuntime:
    def __init__(self, results: dict[str, AgentResult]) -> None:
        self._results = results

    def run(self, case: Case) -> AgentResult:
        return self._results[case.case_id]

    async def run_async(self, case: Case) -> AgentResult:
        return self._results[case.case_id]


def _agent_result(**overrides: object) -> AgentResult:
    defaults: dict[str, object] = {
        "case_id": "case-1",
        "answer": "the answer",
        "evidence": ["ev"],
        "confidence": "high",
        "tool_calls": [],
        "runtime": "agent_sdk",
        "mode": "single",
        "tokens_in": 10,
        "tokens_out": 5,
        "cost_eur": 0.01,
        "elapsed_ms": 100,
        "error": None,
    }
    defaults.update(overrides)
    return AgentResult(**defaults)  # type: ignore[arg-type]


def _patch_judge(monkeypatch: pytest.MonkeyPatch, scores: dict[str, float]) -> None:
    def fake_judge_case(
        case: Case,
        agent_result: AgentResult,
        rubrics: dict[str, Rubric],
        *,
        models_config_path: Path = REPO_MODELS_CONFIG,
    ) -> JudgeResult:
        return JudgeResult(rubric_scores=dict(scores), cost_eur=0.002)

    monkeypatch.setattr(harness, "judge_case", fake_judge_case)


@pytest.mark.unit
def test_score_case_success_uses_judge_and_combines_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_judge(
        monkeypatch,
        {"answer_correctness": 0.8, "evidence_grounding": 1.0, "tool_efficiency": 1.0},
    )
    case = _case("c1")
    runtime = _FakeRuntime({"c1": _agent_result(case_id="c1", cost_eur=0.01)})
    rubrics = {
        "answer_correctness": Rubric(
            dimension="answer_correctness", version=1, weight=0.5, assertions=[]
        )
    }

    score, agent_result = harness.score_case(case, runtime, rubrics)

    assert score.task_completion is True
    assert score.answer_score == 0.8
    assert score.tool_path_exact is True  # N/A default
    assert "N/A" in score.notes
    assert score.cost_eur == pytest.approx(0.01 + 0.002)
    assert agent_result.case_id == "c1"


@pytest.mark.unit
def test_score_case_runtime_error_scores_zero_without_calling_judge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fake_judge_case(*args: object, **kwargs: object) -> JudgeResult:
        nonlocal called
        called = True
        return JudgeResult(rubric_scores={}, cost_eur=0.0)

    monkeypatch.setattr(harness, "judge_case", fake_judge_case)

    case = _case("c2")
    runtime = _FakeRuntime(
        {"c2": _agent_result(case_id="c2", error="boom", answer="", cost_eur=0.0)}
    )
    rubrics = {
        "answer_correctness": Rubric(
            dimension="answer_correctness", version=1, weight=0.5, assertions=[]
        ),
        "evidence_grounding": Rubric(
            dimension="evidence_grounding", version=1, weight=0.3, assertions=[]
        ),
    }

    score, _ = harness.score_case(case, runtime, rubrics)

    assert called is False
    assert score.task_completion is False
    assert score.answer_score == 0.0
    assert score.rubric_scores == {"answer_correctness": 0.0, "evidence_grounding": 0.0}
    assert "runtime error: boom" in score.notes


@pytest.mark.unit
def test_run_evaluation_end_to_end_with_fake_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_judge(monkeypatch, {"answer_correctness": 1.0})
    cases = [
        _case("c1"),
        _case("c2", expected_tool_path=["list_companies"]),
    ]
    runtime = _FakeRuntime(
        {
            "c1": _agent_result(case_id="c1"),
            "c2": _agent_result(
                case_id="c2",
                tool_calls=[],
            ),
        }
    )

    run = harness.run_evaluation(
        cases,
        runtime,
        rubrics_dir=REPO_RUBRICS_DIR,
        prompts_dir=REPO_PROMPTS_DIR,
        models_config_path=REPO_MODELS_CONFIG,
    )

    assert len(run.case_scores) == 2
    assert run.dataset == "finance-agent-bench"
    assert run.rubric_version == harness.RUBRIC_VERSION
    assert run.prompt_hashes  # non-empty, per docs/contracts.md §7
    assert run.aggregate["case_count"] == 2.0
    # c2 has an expected_tool_path but made no tool calls -> not an exact match
    assert run.aggregate["tool_path_applicable_count"] == 1.0
    assert run.aggregate["tool_path_exact_rate"] == 0.0
    assert run.total_cost_eur == pytest.approx(sum(s.cost_eur for s in run.case_scores))


@pytest.mark.unit
def test_run_evaluation_respects_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_judge(monkeypatch, {"answer_correctness": 1.0})
    cases = [_case("c1"), _case("c2"), _case("c3")]
    runtime = _FakeRuntime(
        {cid: _agent_result(case_id=cid) for cid in ["c1", "c2", "c3"]}
    )

    run = harness.run_evaluation(
        cases,
        runtime,
        limit=2,
        rubrics_dir=REPO_RUBRICS_DIR,
        prompts_dir=REPO_PROMPTS_DIR,
        models_config_path=REPO_MODELS_CONFIG,
    )

    assert len(run.case_scores) == 2


@pytest.mark.unit
def test_run_evaluation_hashes_roles_config_for_multi_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """config/roles.yaml governs each role's model/max_turns in multi mode -
    just as load-bearing for reproducibility as models.yaml, so a change to
    it must change model_config_hash when mode="multi".
    """
    _patch_judge(monkeypatch, {"answer_correctness": 1.0})
    case = _case("c1")
    runtime = _FakeRuntime({"c1": _agent_result(case_id="c1", mode="multi")})
    roles_path = tmp_path / "roles.yaml"
    roles_path.write_text("supervisor:\n  model: x\n", encoding="utf-8")

    run_v1 = harness.run_evaluation(
        [case],
        runtime,
        rubrics_dir=REPO_RUBRICS_DIR,
        prompts_dir=REPO_PROMPTS_DIR,
        models_config_path=REPO_MODELS_CONFIG,
        roles_config_path=roles_path,
    )

    roles_path.write_text("supervisor:\n  model: y\n", encoding="utf-8")
    run_v2 = harness.run_evaluation(
        [case],
        runtime,
        rubrics_dir=REPO_RUBRICS_DIR,
        prompts_dir=REPO_PROMPTS_DIR,
        models_config_path=REPO_MODELS_CONFIG,
        roles_config_path=roles_path,
    )

    assert run_v1.model_config_hash != run_v2.model_config_hash


@pytest.mark.unit
def test_run_evaluation_single_mode_ignores_roles_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """roles.yaml is irrelevant to single mode - changing it must not change
    single mode's model_config_hash (no regression to existing hash values).
    """
    _patch_judge(monkeypatch, {"answer_correctness": 1.0})
    case = _case("c1")
    runtime = _FakeRuntime({"c1": _agent_result(case_id="c1", mode="single")})
    roles_path = tmp_path / "roles.yaml"
    roles_path.write_text("supervisor:\n  model: x\n", encoding="utf-8")

    run_v1 = harness.run_evaluation(
        [case],
        runtime,
        rubrics_dir=REPO_RUBRICS_DIR,
        prompts_dir=REPO_PROMPTS_DIR,
        models_config_path=REPO_MODELS_CONFIG,
        roles_config_path=roles_path,
    )

    roles_path.write_text("supervisor:\n  model: y\n", encoding="utf-8")
    run_v2 = harness.run_evaluation(
        [case],
        runtime,
        rubrics_dir=REPO_RUBRICS_DIR,
        prompts_dir=REPO_PROMPTS_DIR,
        models_config_path=REPO_MODELS_CONFIG,
        roles_config_path=roles_path,
    )

    assert run_v1.model_config_hash == run_v2.model_config_hash
