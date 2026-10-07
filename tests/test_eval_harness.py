"""Tests for `eval/harness.py`.

Uses a fake `Runtime` (implements both `.run(case)` and `.run_async(case)` per
`runtimes/base.py`; the harness only calls `.run`) and a monkeypatched
`judge_case` so no real model call happens here — the judge itself is
covered separately in `tests/test_eval_judge.py`.
"""

from pathlib import Path

import pytest
from claude_agent_sdk import ProcessError, ResultError

from recon.adapters.finance_agent_bench import DATASET_ID
from recon.contracts import AgentResult, Case, EvalRun, ToolCall
from recon.eval import gate, harness, judge_failures, scoring
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

    monkeypatch.setattr(scoring, "judge_case", fake_judge_case)


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

    score, agent_result = scoring.score_case(case, runtime, rubrics)

    assert score.task_completion is True
    assert score.answer_score == 0.8
    assert score.tool_path_exact is True  # N/A default
    assert "N/A" in score.notes
    assert score.cost_eur == pytest.approx(0.01 + 0.002)
    assert agent_result.case_id == "c1"


@pytest.mark.unit
def test_score_case_records_the_tool_names_in_call_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#101: a result file shows which tools each case called."""
    _patch_judge(monkeypatch, {"answer_correctness": 1.0})
    calls = [
        ToolCall(tool=name, arguments={}, status="ok", elapsed_ms=1)
        for name in ("list_companies", "search_knowledge", "search_knowledge")
    ]
    runtime = _FakeRuntime({"c1": _agent_result(case_id="c1", tool_calls=calls)})

    score, _ = scoring.score_case(_case("c1"), runtime, {})

    assert score.tool_names == [
        "list_companies",
        "search_knowledge",
        "search_knowledge",
    ]


@pytest.mark.unit
def test_score_case_answer_score_is_weighted_across_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_judge(
        monkeypatch,
        {"answer_correctness": 0.2, "evidence_grounding": 1.0, "tool_efficiency": 0.5},
    )
    case = _case("c5")
    runtime = _FakeRuntime({"c5": _agent_result(case_id="c5")})
    rubrics = {
        dimension: Rubric(dimension=dimension, version=1, weight=weight, assertions=[])
        for dimension, weight in [
            ("answer_correctness", 0.5),
            ("evidence_grounding", 0.3),
            ("tool_efficiency", 0.2),
        ]
    }

    score, _ = scoring.score_case(case, runtime, rubrics)

    assert score.answer_score == pytest.approx(0.5 * 0.2 + 0.3 * 1.0 + 0.2 * 0.5)
    assert score.rubric_scores["answer_correctness"] == 0.2


@pytest.mark.unit
def test_score_case_runtime_error_without_answer_scores_zero_without_judge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fake_judge_case(*args: object, **kwargs: object) -> JudgeResult:
        nonlocal called
        called = True
        return JudgeResult(rubric_scores={}, cost_eur=0.0)

    monkeypatch.setattr(scoring, "judge_case", fake_judge_case)

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

    score, _ = scoring.score_case(case, runtime, rubrics)

    assert called is False
    assert score.task_completion is False
    assert score.answer_score == 0.0
    assert score.rubric_scores == {"answer_correctness": 0.0, "evidence_grounding": 0.0}
    assert "runtime error: boom" in score.notes
    assert scoring.JUDGED_DESPITE_ERROR_NOTE not in score.notes


@pytest.mark.unit
@pytest.mark.parametrize(
    "error",
    [
        # agent_sdk.py single mode: token budget checked after the one call.
        (
            "token budget of 300000 exceeded (310000 used) - reported after the "
            "fact, since single mode's one call had already completed"
        ),
        # langgraph.py multi mode: paused before the review-flag write.
        (
            "paused for review-flag confirmation (thread_id='t-1') - call "
            "LangGraphRuntime.resume(case, thread_id, approved=...) to continue"
        ),
    ],
)
def test_score_case_judges_answer_despite_non_fatal_error(
    monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    _patch_judge(monkeypatch, {"answer_correctness": 0.9})
    case = _case("c3")
    runtime = _FakeRuntime(
        {"c3": _agent_result(case_id="c3", error=error, cost_eur=0.01)}
    )
    rubrics = {
        "answer_correctness": Rubric(
            dimension="answer_correctness", version=1, weight=0.5, assertions=[]
        )
    }

    score, _ = scoring.score_case(case, runtime, rubrics)

    assert score.answer_score == 0.9
    assert score.rubric_scores == {"answer_correctness": 0.9}
    assert score.cost_eur == pytest.approx(0.01 + 0.002)
    # task_completion stays strict: the run didn't finish within its constraints.
    assert score.task_completion is False
    assert f"runtime error: {error}" in score.notes
    assert scoring.JUDGED_DESPITE_ERROR_NOTE in score.notes


@pytest.mark.unit
def test_score_case_whitespace_answer_with_error_is_hard_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fake_judge_case(*args: object, **kwargs: object) -> JudgeResult:
        nonlocal called
        called = True
        return JudgeResult(rubric_scores={}, cost_eur=0.0)

    monkeypatch.setattr(scoring, "judge_case", fake_judge_case)
    case = _case("c4")
    runtime = _FakeRuntime(
        {"c4": _agent_result(case_id="c4", error="boom", answer="  \n")}
    )
    rubrics = {
        "answer_correctness": Rubric(
            dimension="answer_correctness", version=1, weight=0.5, assertions=[]
        )
    }

    score, _ = scoring.score_case(case, runtime, rubrics)

    assert called is False
    assert score.answer_score == 0.0
    assert scoring.JUDGED_DESPITE_ERROR_NOTE not in score.notes


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
        tool_data_snapshot="20260928",
    )

    assert len(run.case_scores) == 2
    assert run.dataset == DATASET_ID
    assert run.dataset.startswith("finance-agent-bench@")
    assert run.tool_data_snapshot == "20260928"
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


def _run_three(monkeypatch: pytest.MonkeyPatch, max_cost_eur: float | None) -> EvalRun:
    """Three cases at €0.012 each: €0.01 agent, €0.002 judge."""
    _patch_judge(monkeypatch, {"answer_correctness": 1.0})
    ids = ["c1", "c2", "c3"]
    runtime = _FakeRuntime({cid: _agent_result(case_id=cid) for cid in ids})
    return harness.run_evaluation(
        [_case(cid) for cid in ids],
        runtime,
        rubrics_dir=REPO_RUBRICS_DIR,
        prompts_dir=REPO_PROMPTS_DIR,
        models_config_path=REPO_MODELS_CONFIG,
        max_cost_eur=max_cost_eur,
    )


@pytest.mark.unit
def test_run_evaluation_splits_agent_and_judge_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run_three(monkeypatch, None)
    assert run.aggregate["agent_cost_eur"] == pytest.approx(0.03)
    assert run.aggregate["judge_cost_eur"] == pytest.approx(0.006)
    assert harness.SKIPPED_AT_COST_CAP not in run.aggregate


@pytest.mark.unit
def test_run_evaluation_stops_before_a_case_that_could_pass_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # After two cases €0.024 is spent; a third like the dearest so far would
    # make €0.036, past €0.03.
    run = _run_three(monkeypatch, 0.03)
    assert [s.case_id for s in run.case_scores] == ["c1", "c2"]
    assert run.total_cost_eur == pytest.approx(0.024)
    assert run.aggregate["case_count"] == 2.0
    assert run.aggregate[harness.SKIPPED_AT_COST_CAP] == 1.0


@pytest.mark.unit
def test_run_evaluation_always_runs_the_first_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing is known about cost before the first case; run_budget bounds it."""
    run = _run_three(monkeypatch, 0.001)
    assert len(run.case_scores) == 1
    assert run.aggregate[harness.SKIPPED_AT_COST_CAP] == 2.0


@pytest.mark.unit
def test_run_evaluation_within_the_cap_runs_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run_three(monkeypatch, 0.05)
    assert len(run.case_scores) == 3
    assert harness.SKIPPED_AT_COST_CAP not in run.aggregate


@pytest.mark.unit
def test_run_evaluation_reports_each_case_as_it_finishes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A paid run took 20 minutes with no output until the end."""
    _run_three(monkeypatch, max_cost_eur=None)

    lines = capsys.readouterr().out.splitlines()
    assert [line.split(":")[0] for line in lines] == [
        "case 1/3 c1",
        "case 2/3 c2",
        "case 3/3 c3",
    ]
    assert "completed" in lines[0]
    assert "€0.01" in lines[0]


def _cited(verified: bool) -> dict[str, object]:
    return {
        "claims": [{"text": "c", "importance": "key", "evidence_refs": ["Ea"]}],
        "evidence_items": [
            {
                "ref": "Ea",
                "verified": verified,
                "source_type": "filing_text" if verified else "unknown",
            }
        ],
    }


@pytest.mark.unit
def test_score_case_records_the_citation_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0030: deterministic, next to the judge's evidence_grounding."""
    _patch_judge(monkeypatch, {"answer_correctness": 1.0})
    runtime = _FakeRuntime({"c1": _agent_result(case_id="c1", **_cited(False))})

    score, _ = scoring.score_case(_case("c1"), runtime, {})

    assert score.claim_support_rate == 0.0
    assert score.citation_precision == 0.0


@pytest.mark.unit
def test_run_aggregates_citation_metrics_over_the_cases_that_have_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_judge(monkeypatch, {"answer_correctness": 1.0})
    runtime = _FakeRuntime(
        {
            "c1": _agent_result(case_id="c1", **_cited(True)),
            "c2": _agent_result(case_id="c2", **_cited(False)),
            "c3": _agent_result(case_id="c3"),
        }
    )

    run = harness.run_evaluation(
        [_case("c1"), _case("c2"), _case("c3")],
        runtime,
        rubrics_dir=REPO_RUBRICS_DIR,
        prompts_dir=REPO_PROMPTS_DIR,
        models_config_path=REPO_MODELS_CONFIG,
        tool_data_snapshot="20260928",
    )

    assert run.aggregate["claim_support_rate_mean"] == pytest.approx(0.5)
    assert run.aggregate["citation_precision_mean"] == pytest.approx(0.5)
    assert run.aggregate["citation_scored_cases"] == 2.0


@pytest.mark.unit
def test_the_rubric_version_is_4_for_claims() -> None:
    """ADR 0030: the judges see resolved evidence, so version 3 scores aren't comparable."""
    assert harness.RUBRIC_VERSION == "4"


# --- judge failures (#123) ----------------------------------------------------

SESSION_LIMIT_TEXT = "You've hit your session limit · resets 6:50pm"


def _result_error(text: str) -> ResultError:
    """What the SDK raises when the CLI reports an error result and exits."""
    return ResultError(
        f"Claude Code returned an error result: {text}",
        data={"subtype": "success", "is_error": True, "result": text},
        exit_code=1,
    )


def _patch_failing_judge(
    monkeypatch: pytest.MonkeyPatch, failing_case: str, error: Exception
) -> None:
    def fake_judge_case(
        case: Case,
        agent_result: AgentResult,
        rubrics: dict[str, Rubric],
        *,
        models_config_path: Path = REPO_MODELS_CONFIG,
    ) -> JudgeResult:
        if case.case_id == failing_case:
            raise error
        return JudgeResult(rubric_scores={"answer_correctness": 1.0}, cost_eur=0.002)

    monkeypatch.setattr(scoring, "judge_case", fake_judge_case)


def _run_ids(ids: list[str], runtime: _FakeRuntime | None = None) -> EvalRun:
    return harness.run_evaluation(
        [_case(cid) for cid in ids],
        runtime or _FakeRuntime({cid: _agent_result(case_id=cid) for cid in ids}),
        rubrics_dir=REPO_RUBRICS_DIR,
        prompts_dir=REPO_PROMPTS_DIR,
        models_config_path=REPO_MODELS_CONFIG,
    )


@pytest.mark.unit
def test_a_session_limit_in_the_judge_stops_the_run_and_keeps_the_scored_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#123: the run crashed in case 3 and lost the two cases already scored."""
    _patch_failing_judge(monkeypatch, "c2", _result_error(SESSION_LIMIT_TEXT))

    run = _run_ids(["c1", "c2", "c3"])

    assert [s.case_id for s in run.case_scores] == ["c1", "c2"]
    first, second = run.case_scores
    assert first.rubric_scores == {"answer_correctness": 1.0}
    assert first.judge_failed is False
    assert second.judge_failed is True
    assert "session limit" in second.notes
    # The agent's share of c2 was spent, so it still counts.
    assert run.total_cost_eur == pytest.approx(0.012 + 0.01)
    assert run.aggregate[gate.SKIPPED_AT_SESSION_LIMIT] == 1.0
    assert run.aggregate[gate.CASES_JUDGE_FAILED] == 1.0


@pytest.mark.unit
def test_another_judge_failure_unscores_that_case_and_the_run_goes_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_failing_judge(monkeypatch, "c2", _result_error("API Error: overloaded"))

    run = _run_ids(["c1", "c2", "c3"])

    assert [s.case_id for s in run.case_scores] == ["c1", "c2", "c3"]
    failed = run.case_scores[1]
    assert failed.judge_failed is True
    assert failed.rubric_scores == {}
    assert failed.answer_score == 0.0
    assert "judge failed: " in failed.notes and "overloaded" in failed.notes
    assert run.aggregate[gate.CASES_JUDGE_FAILED] == 1.0
    assert gate.SKIPPED_AT_SESSION_LIMIT not in run.aggregate


@pytest.mark.unit
def test_a_process_error_in_the_judge_is_caught_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_failing_judge(monkeypatch, "c1", ProcessError("CLI died", exit_code=1))

    run = _run_ids(["c1", "c2"])

    assert run.case_scores[0].judge_failed is True
    assert len(run.case_scores) == 2


@pytest.mark.unit
def test_a_session_limit_in_the_agent_stops_the_run_after_that_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agent hits the same limit first when a case starts after it."""
    _patch_judge(monkeypatch, {"answer_correctness": 1.0})
    runtime = _FakeRuntime(
        {
            "c1": _agent_result(case_id="c1"),
            "c2": _agent_result(
                case_id="c2",
                answer="",
                error=f"Claude Code returned an error result: {SESSION_LIMIT_TEXT}",
            ),
            "c3": _agent_result(case_id="c3"),
        }
    )

    run = _run_ids(["c1", "c2", "c3"], runtime)

    assert [s.case_id for s in run.case_scores] == ["c1", "c2"]
    assert run.aggregate[gate.SKIPPED_AT_SESSION_LIMIT] == 1.0
    assert gate.CASES_JUDGE_FAILED not in run.aggregate


@pytest.mark.unit
def test_a_faithfulness_judge_failure_keeps_the_rubric_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_judge(monkeypatch, {"answer_correctness": 0.8})

    def failing_faithfulness(*args: object, **kwargs: object) -> None:
        raise _result_error("API Error: overloaded")

    monkeypatch.setattr(
        scoring.faithfulness, "judge_faithfulness", failing_faithfulness
    )
    runtime = _FakeRuntime({"c1": _agent_result(case_id="c1")})

    score, _ = scoring.score_case(
        _case("c1"), runtime, {}, passages=lambda query, top_k, company_id: []
    )

    assert score.rubric_scores == {"answer_correctness": 0.8}
    assert score.judge_failed is False
    assert "faithfulness judge failed: " in score.notes


@pytest.mark.unit
def test_score_case_raises_with_the_scored_case_at_a_session_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callers outside the harness's loop still get the case that ran."""
    _patch_failing_judge(monkeypatch, "c1", _result_error(SESSION_LIMIT_TEXT))
    runtime = _FakeRuntime({"c1": _agent_result(case_id="c1")})

    with pytest.raises(judge_failures.SessionLimitReached) as caught:
        scoring.score_case(_case("c1"), runtime, {})

    assert caught.value.score.case_id == "c1"
    assert caught.value.score.judge_failed is True


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (SESSION_LIMIT_TEXT, True),
        ("Claude AI usage limit reached|1760000000", True),
        ("API Error: 429 rate_limit_error", True),
        ("API Error: overloaded", False),
        ("tool-call budget of 12 exceeded", False),
    ],
)
def test_is_session_limit(text: str, expected: bool) -> None:
    assert judge_failures.is_session_limit(text) is expected


# --- run-path breakdown and failure classes (#116) ---------------------------


@pytest.mark.unit
def test_every_scored_case_gets_a_breakdown_and_a_failure_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_judge(monkeypatch, {"answer_correctness": 0.2, "evidence_grounding": 0.2})
    runtime = _FakeRuntime(
        {
            "c1": _agent_result(
                case_id="c1",
                tool_calls=[
                    ToolCall(
                        tool="list_companies", arguments={}, status="ok", elapsed_ms=1
                    )
                ],
            ),
            "c2": _agent_result(case_id="c2", answer="", error="CLI crashed"),
        }
    )

    run = harness.run_evaluation(
        [_case("c1"), _case("c2")],
        runtime,
        rubrics_dir=REPO_RUBRICS_DIR,
        prompts_dir=REPO_PROMPTS_DIR,
        models_config_path=REPO_MODELS_CONFIG,
        correct_answer_score=0.5,
    )

    first, second = run.case_scores
    assert first.trajectory is not None
    assert first.trajectory.final_correctness == 0.2
    assert first.trajectory.argument_correctness == 1.0
    assert first.failure_class == "reasoning"
    assert second.failure_class == "runtime_error"
    assert second.failure_reason == "CLI crashed"
    assert run.aggregate["failure_reasoning_count"] == 1.0
    assert run.aggregate["failure_runtime_error_count"] == 1.0
    assert run.aggregate["failure_classified_cases"] == 2.0


@pytest.mark.unit
def test_the_breakdown_scores_retrieval_on_labelled_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_judge(monkeypatch, {"answer_correctness": 1.0})
    monkeypatch.setattr(
        scoring.faithfulness,
        "judge_faithfulness",
        lambda *a, **k: scoring.faithfulness.FaithfulnessResult(score=None),
    )
    search = ToolCall(
        tool="search_knowledge", arguments={"query": "q"}, status="ok", elapsed_ms=1
    )
    runtime = _FakeRuntime({"c1": _agent_result(case_id="c1", tool_calls=[search])})

    rubrics = {
        "answer_correctness": Rubric(
            dimension="answer_correctness", version=1, weight=1.0, assertions=[]
        )
    }

    score, _ = scoring.score_case(
        _case("c1"),
        runtime,
        rubrics,
        passages=lambda query, top_k, company_id: [{"chunk_id": "a"}],
        retrieval_labels={"c1": ["a", "b"]},
        correct_answer_score=0.5,
    )

    assert score.trajectory is not None
    assert score.trajectory.retrieval_quality is not None
    assert score.trajectory.retrieval_quality.recall == 0.5
    assert score.failure_class == "none"
