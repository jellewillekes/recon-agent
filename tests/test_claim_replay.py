"""Tests for verifying an answer's claims by replaying its fact calls (#139,
ADR 0038): `tools/fact_replay.py`, `eval/claim_replay.py` and the harness.

Fact rows come from the fixture DuckDB (`RECON_TOOL_DATA=fixture`), so the
refs are the ones the MCP server would hand the agent. The judge is
monkeypatched; nothing here calls a model.
"""

from pathlib import Path
from typing import Any

import pytest

from recon.contracts import AgentResult, Case, Claim, ToolCall
from recon.eval import claim_replay, harness, scoring
from recon.eval.claim_verifier import verifier_version
from recon.eval.judge import JudgeResult
from recon.eval.rubrics import Rubric
from recon.tools.data_source import open_tool_data
from recon.tools.fact_replay import replay_facts
from recon.tools.refs import row_ref

REPO_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

REVENUE_2024 = {
    "company_id": "FIRM-001",
    "concept": "revenue",
    "fiscal_year": 2024,
    "fiscal_period": "FY",
}


@pytest.fixture
def facts() -> claim_replay.Facts:
    return replay_facts(open_tool_data())


def _revenue_ref(facts: claim_replay.Facts) -> str:
    [row] = facts(REVENUE_2024)
    return str(row["ref"])


def _fact_call(arguments: dict[str, Any]) -> ToolCall:
    return ToolCall(
        tool="get_financial_fact", arguments=arguments, status="ok", elapsed_ms=1
    )


def _claim(text: str, refs: list[str]) -> Claim:
    return Claim(text=text, importance="key", evidence_refs=refs)


def _agent_result(case_id: str = "c1", **overrides: object) -> AgentResult:
    defaults: dict[str, object] = {
        "case_id": case_id,
        "answer": "the answer",
        "evidence": [],
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


def _case(case_id: str) -> Case:
    return Case(
        case_id=case_id,
        source="finance-agent-bench",
        question="q",
        expected_answer="a",
        expected_tool_path=None,
        context={"rubric": []},
        tags=["Trends"],
        license="MIT",
        attribution="attribution",
    )


class _FakeRuntime:
    def __init__(self, results: list[AgentResult]) -> None:
        self._results = {result.case_id: result for result in results}

    def run(self, case: Case) -> AgentResult:
        return self._results[case.case_id]

    async def run_async(self, case: Case) -> AgentResult:
        return self._results[case.case_id]


def _count_judge_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def fake_judge_case(
        case: Case,
        agent_result: AgentResult,
        rubrics: dict[str, Rubric],
        *,
        models_config_path: Path,
    ) -> JudgeResult:
        calls.append(case.case_id)
        return JudgeResult(rubric_scores={"answer_correctness": 1.0}, cost_eur=0.0)

    monkeypatch.setattr(scoring, "judge_case", fake_judge_case)
    return calls


@pytest.mark.unit
def test_replay_returns_the_rows_with_the_refs_the_server_gives(
    facts: claim_replay.Facts,
) -> None:
    [row] = facts(REVENUE_2024)
    content = {key: value for key, value in row.items() if key != "ref"}
    assert row["ref"] == row_ref("get_financial_fact", content, "FIRM-001")
    assert row["value"] == 550.0


@pytest.mark.unit
def test_replay_of_a_call_the_server_would_refuse_returns_no_rows(
    facts: claim_replay.Facts,
) -> None:
    assert facts({"concept": "revenue"}) == []
    assert facts({**REVENUE_2024, "company_id": "NOPE-1"}) == []


@pytest.mark.unit
def test_claims_are_checked_against_the_replayed_rows(
    facts: claim_replay.Facts,
) -> None:
    ref = _revenue_ref(facts)
    result = _agent_result(
        tool_calls=[_fact_call(REVENUE_2024)],
        claims=[
            _claim("Revenue was $550 million in FY2024", [ref]),
            _claim("Revenue was $600 million in FY2024", [ref]),
        ],
    )

    verdicts = [v.verdict for v in claim_replay.verify_answer(result, facts)]

    assert verdicts == ["SUPPORTED", "CONTRADICTED"]


@pytest.mark.unit
def test_a_ref_no_fact_call_returned_is_unsupported(
    facts: claim_replay.Facts,
) -> None:
    result = _agent_result(
        tool_calls=[_fact_call(REVENUE_2024)],
        claims=[_claim("Revenue was $550 million in FY2024", ["E000000000000"])],
    )

    [verification] = claim_replay.verify_answer(result, facts)

    assert verification.verdict == "UNSUPPORTED"


@pytest.mark.unit
def test_each_distinct_fact_call_is_replayed_once() -> None:
    seen: list[dict[str, Any]] = []

    def facts(arguments: dict[str, Any]) -> list[dict[str, Any]]:
        seen.append(arguments)
        return []

    other_year = {**REVENUE_2024, "fiscal_year": 2023}
    search = ToolCall(
        tool="search_knowledge", arguments={"query": "x"}, status="ok", elapsed_ms=1
    )
    result = _agent_result(
        tool_calls=[
            _fact_call(REVENUE_2024),
            search,
            _fact_call(dict(REVENUE_2024)),
            _fact_call(other_year),
        ],
        claims=[_claim("Revenue was $550 million in FY2024", [])],
    )

    claim_replay.verify_answer(result, facts)

    assert seen == [REVENUE_2024, other_year]


@pytest.mark.unit
def test_an_answer_without_claims_gets_an_empty_list(
    facts: claim_replay.Facts,
) -> None:
    result = _agent_result(tool_calls=[_fact_call(REVENUE_2024)])
    assert claim_replay.verify_answer(result, facts) == []


@pytest.mark.unit
def test_every_scored_case_is_verified_and_the_run_records_the_verifier(
    monkeypatch: pytest.MonkeyPatch, facts: claim_replay.Facts
) -> None:
    judged = _count_judge_calls(monkeypatch)
    ref = _revenue_ref(facts)
    with_claims = _agent_result(
        "c1",
        tool_calls=[_fact_call(REVENUE_2024)],
        claims=[_claim("Revenue was $600 million in FY2024", [ref])],
    )
    without_claims = _agent_result("c2")

    run = harness.run_evaluation(
        [_case("c1"), _case("c2")],
        _FakeRuntime([with_claims, without_claims]),
        prompts_dir=REPO_PROMPTS_DIR,
        facts=facts,
    )

    by_id = {case.case_id: case for case in run.case_scores}
    assert [v.verdict for v in by_id["c1"].verifications or []] == ["CONTRADICTED"]
    assert by_id["c2"].verifications == []
    assert run.verifier_version == verifier_version()
    # Replay is a database query, not a model call: one judge call per case.
    assert judged == ["c1", "c2"]


@pytest.mark.unit
def test_a_run_without_replay_leaves_claims_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _count_judge_calls(monkeypatch)
    result = _agent_result(claims=[_claim("Revenue was $550 million in FY2024", [])])

    run = harness.run_evaluation(
        [_case("c1")], _FakeRuntime([result]), prompts_dir=REPO_PROMPTS_DIR
    )

    assert run.case_scores[0].verifications is None
    assert run.verifier_version is None
