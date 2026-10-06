"""Tests for `eval/faithfulness.py` and how the harness records it.

The judge query is monkeypatched with a fixed structured answer, and the
search replay is a fake, so nothing here calls a model or a database.
"""

from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions

from recon.contracts import AgentResult, Case, ToolCall
from recon.eval import faithfulness, harness
from recon.eval.judge import JudgeResult, StructuredCall
from recon.eval.rubrics import Rubric

REPO_MODELS_CONFIG = Path(__file__).parent.parent / "config" / "models.yaml"

CASE = Case(
    case_id="finance-agent-bench:1",
    source="finance-agent-bench",
    question="What did the company guide for next quarter?",
    expected_answer="Revenue of $1.0B to $1.1B.",
    expected_tool_path=None,
    context={},
    tags=["Beat or Miss"],
    license="MIT",
    attribution="attribution",
)


def _passage(
    chunk_id: str, text: str = "Guidance: revenue of $1.0B to $1.1B."
) -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "form": "8-K",
        "filed": "2024-05-01",
        "section": "EX-99.1",
        "text": text,
    }


def _search(query: str, top_k: int = 5, status: str = "ok") -> ToolCall:
    return ToolCall(
        tool="search_knowledge",
        arguments={"query": query, "top_k": top_k},
        status=status,
        elapsed_ms=5,
    )


def _agent_result(
    tool_calls: list[ToolCall], answer: str = "Revenue of $1.0B to $1.1B."
) -> AgentResult:
    return AgentResult(
        case_id=CASE.case_id,
        answer=answer,
        evidence=["8-K EX-99.1 filed 2024-05-01"],
        confidence="high",
        tool_calls=tool_calls,
        runtime="agent_sdk",
        mode="single",
        tokens_in=10,
        tokens_out=5,
        cost_eur=0.01,
        elapsed_ms=100,
        error=None,
    )


def _patch_query(
    monkeypatch: pytest.MonkeyPatch, claims: list[dict[str, Any]]
) -> list[str]:
    """Answer every judge query with `claims`. Returns the prompts it got."""
    prompts: list[str] = []

    async def fake_structured_query(
        prompt: str, options: ClaudeAgentOptions, required_key: str
    ) -> StructuredCall:
        prompts.append(prompt)
        return StructuredCall(
            structured={required_key: claims},
            cost_usd=0.01,
            tokens_in=100,
            tokens_out=20,
            num_turns=1,
        )

    monkeypatch.setattr(faithfulness, "structured_query", fake_structured_query)
    return prompts


@pytest.fixture
def prompt_path(tmp_path: Path) -> Path:
    path = tmp_path / "judge_faithfulness.md"
    path.write_text("You are a strict grader.", encoding="utf-8")
    return path


@pytest.mark.unit
def test_searched_queries_keeps_only_searches_that_returned_passages() -> None:
    result = _agent_result(
        [
            _search("guidance", top_k=3),
            _search("risks", status="empty"),
            ToolCall(
                tool="get_financial_fact",
                arguments={"query": "x"},
                status="ok",
                elapsed_ms=1,
            ),
            ToolCall(
                tool="search_knowledge",
                arguments={"query": "no top_k"},
                status="ok",
                elapsed_ms=1,
            ),
        ]
    )

    assert faithfulness.searched_queries(result) == [("guidance", 3), ("no top_k", 5)]


@pytest.mark.unit
def test_replay_returns_each_passage_once_in_first_seen_order() -> None:
    returned = {
        "first": [_passage("a"), _passage("b")],
        "second": [_passage("b"), _passage("c")],
    }
    asked: list[tuple[str, int]] = []

    def passages(query: str, top_k: int) -> list[dict[str, Any]]:
        asked.append((query, top_k))
        return returned[query]

    result = _agent_result([_search("first", top_k=2), _search("second", top_k=4)])

    replayed = faithfulness.replay(result, passages)

    assert [p["chunk_id"] for p in replayed] == ["a", "b", "c"]
    assert asked == [("first", 2), ("second", 4)]


@pytest.mark.unit
def test_score_is_the_share_of_supported_claims() -> None:
    claims = [
        {"claim": "a", "supported": True},
        {"claim": "b", "supported": True},
        {"claim": "c", "supported": False},
        {"claim": "d", "supported": True},
    ]

    assert faithfulness.score(claims) == 0.75


@pytest.mark.unit
def test_score_is_none_when_the_answer_made_no_claim_from_filing_text() -> None:
    assert faithfulness.score([]) is None


@pytest.mark.unit
def test_no_search_means_no_judge_call_and_no_score(
    monkeypatch: pytest.MonkeyPatch, prompt_path: Path
) -> None:
    prompts = _patch_query(monkeypatch, [{"claim": "a", "supported": True}])

    result = faithfulness.judge_faithfulness(
        CASE,
        _agent_result([]),
        lambda query, top_k: [_passage("a")],
        models_config_path=REPO_MODELS_CONFIG,
        prompt_path=prompt_path,
    )

    assert result.score is None
    assert result.cost_eur == 0.0
    assert prompts == []


@pytest.mark.unit
def test_judge_sees_the_answer_and_every_replayed_passage(
    monkeypatch: pytest.MonkeyPatch, prompt_path: Path
) -> None:
    prompts = _patch_query(
        monkeypatch,
        [
            {"claim": "guidance", "supported": True},
            {"claim": "margin", "supported": False},
        ],
    )

    result = faithfulness.judge_faithfulness(
        CASE,
        _agent_result([_search("guidance")]),
        lambda query, top_k: [
            _passage("acc:EX-99.1:0"),
            _passage("acc:EX-99.1:1", "Margin rose."),
        ],
        models_config_path=REPO_MODELS_CONFIG,
        prompt_path=prompt_path,
    )

    assert result.score == 0.5
    assert result.cost_eur > 0
    assert "Revenue of $1.0B to $1.1B." in prompts[0]
    assert "acc:EX-99.1:0" in prompts[0]
    assert "Margin rose." in prompts[0]


class _FakeRuntime:
    def __init__(self, result: AgentResult) -> None:
        self._result = result

    def run(self, case: Case) -> AgentResult:
        return self._result

    async def run_async(self, case: Case) -> AgentResult:
        return self._result


def _patch_rubric_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_judge_case(
        case: Case,
        agent_result: AgentResult,
        rubrics: dict[str, Rubric],
        *,
        models_config_path: Path = REPO_MODELS_CONFIG,
    ) -> JudgeResult:
        return JudgeResult(rubric_scores={"answer_correctness": 0.8}, cost_eur=0.002)

    monkeypatch.setattr(harness, "judge_case", fake_judge_case)


def _patch_faithfulness(monkeypatch: pytest.MonkeyPatch, score: float | None) -> None:
    def fake(
        case: Case,
        agent_result: AgentResult,
        passages: faithfulness.Passages,
        *,
        models_config_path: Path,
    ) -> faithfulness.FaithfulnessResult:
        return faithfulness.FaithfulnessResult(
            score=score, cost_eur=0.003 if score is not None else 0.0
        )

    monkeypatch.setattr(faithfulness, "judge_faithfulness", fake)


RUBRICS = {
    "answer_correctness": Rubric(
        dimension="answer_correctness", version=1, weight=0.5, assertions=[]
    )
}


@pytest.mark.unit
def test_harness_records_faithfulness_outside_the_answer_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_rubric_judge(monkeypatch)
    _patch_faithfulness(monkeypatch, 0.25)
    runtime = _FakeRuntime(_agent_result([_search("guidance")]))

    score, _ = harness.score_case(
        CASE, runtime, RUBRICS, passages=lambda query, top_k: []
    )

    assert score.rubric_scores["faithfulness"] == 0.25
    assert score.answer_score == 0.8
    assert score.cost_eur == pytest.approx(0.01 + 0.002 + 0.003)


@pytest.mark.unit
def test_harness_leaves_faithfulness_out_without_a_score_or_a_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_rubric_judge(monkeypatch)
    _patch_faithfulness(monkeypatch, None)
    runtime = _FakeRuntime(_agent_result([]))

    with_replay, _ = harness.score_case(
        CASE, runtime, RUBRICS, passages=lambda query, top_k: []
    )
    without_replay, _ = harness.score_case(CASE, runtime, RUBRICS)

    assert "faithfulness" not in with_replay.rubric_scores
    assert "faithfulness" not in without_replay.rubric_scores


@pytest.mark.unit
def test_faithfulness_mean_covers_only_the_cases_that_were_scored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_rubric_judge(monkeypatch)
    scores = iter([1.0, None, 0.5])

    def fake(
        case: Case,
        agent_result: AgentResult,
        passages: faithfulness.Passages,
        *,
        models_config_path: Path,
    ) -> faithfulness.FaithfulnessResult:
        return faithfulness.FaithfulnessResult(score=next(scores))

    monkeypatch.setattr(faithfulness, "judge_faithfulness", fake)
    runtime = _FakeRuntime(_agent_result([_search("guidance")]))
    cases = [CASE.model_copy(update={"case_id": f"c{i}"}) for i in range(3)]

    run = harness.run_evaluation(cases, runtime, passages=lambda query, top_k: [])

    assert run.aggregate["faithfulness_mean"] == 0.75
    assert run.aggregate["faithfulness_scored_cases"] == 2
    assert run.aggregate["answer_score_mean"] == pytest.approx(0.8)
