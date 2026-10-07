"""Trace spans from an eval run and the API (step 12, #17), captured in memory.

OpenTelemetry allows one global tracer provider per process, so the provider
is installed once for this module and each test clears what it captured. No
network: the exporter is in-memory, the runtime and judge are fakes.
"""

from itertools import pairwise
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode

from recon import tracing
from recon.contracts import AgentResult, Case, ToolCall
from recon.eval import harness, scoring
from recon.eval.judge import JudgeResult

ROOT = Path(__file__).resolve().parent.parent
EXPORTER = InMemorySpanExporter()

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module", autouse=True)
def _provider() -> None:
    tracing.configure_tracing("recon-test", exporter=EXPORTER)


@pytest.fixture
def fresh_exporter() -> None:
    """Start each test with no captured spans."""
    EXPORTER.clear()


def _finished() -> list[ReadableSpan]:
    trace.get_tracer_provider().force_flush()  # type: ignore[attr-defined]
    return list(EXPORTER.get_finished_spans())


def _case() -> Case:
    return Case(
        case_id="c1",
        source="finance-agent-bench",
        question="q",
        expected_answer="a",
        expected_tool_path=None,
        context={"rubric": []},
        tags=[],
        license="MIT",
        attribution="attribution",
    )


def _result(tool_calls: list[ToolCall], answer: str = "the answer") -> AgentResult:
    return AgentResult(
        case_id="c1",
        answer=answer,
        evidence=["ev"],
        confidence="high",
        tool_calls=tool_calls,
        runtime="agent_sdk",
        mode="single",
        tokens_in=1200,
        tokens_out=300,
        cost_eur=0.07,
        elapsed_ms=5000,
        error=None,
    )


class _Runtime:
    def __init__(self, result: AgentResult) -> None:
        self._result = result

    def run(self, case: Case) -> AgentResult:
        return self._result

    async def run_async(self, case: Case) -> AgentResult:
        return self._result


def _run_one_case(monkeypatch: pytest.MonkeyPatch, result: AgentResult) -> None:
    def fake_judge(*args: object, **kwargs: object) -> JudgeResult:
        return JudgeResult(
            rubric_scores={"answer_correctness": 1.0},
            cost_eur=0.02,
            model="claude-haiku-4-5-20251001",
            tokens_in=900,
            tokens_out=40,
            num_turns=2,
        )

    monkeypatch.setattr(scoring, "judge_case", fake_judge)
    harness.run_evaluation(
        [_case()],
        _Runtime(result),
        rubrics_dir=ROOT / "config" / "rubrics",
        prompts_dir=ROOT / "prompts",
        models_config_path=ROOT / "config" / "models.yaml",
    )


TOOLS = [
    ToolCall(tool="get_financial_facts", arguments={}, status="ok", elapsed_ms=30),
    ToolCall(tool="get_filings", arguments={}, status="unavailable", elapsed_ms=20),
    ToolCall(tool="list_companies", arguments={}, status="empty", elapsed_ms=10),
]


def test_tracing_is_off_without_an_endpoint() -> None:
    assert tracing.configure_tracing("recon-test") is False


def test_an_eval_run_traces_run_case_agent_tools_and_judge(
    monkeypatch: pytest.MonkeyPatch, fresh_exporter: None
) -> None:
    _run_one_case(monkeypatch, _result(TOOLS))
    by_name = {s.name: s for s in _finished()}
    assert set(by_name) == {
        "eval.run",
        "eval.case",
        "invoke_agent",
        "chat judge",
        "execute_tool get_financial_facts",
        "execute_tool get_filings",
        "execute_tool list_companies",
    }

    def parent(name: str) -> str:
        span_parent = by_name[name].parent
        assert span_parent is not None
        return next(
            s.name for s in by_name.values() if s.context.span_id == span_parent.span_id
        )

    assert parent("eval.case") == "eval.run"
    assert parent("invoke_agent") == "eval.case"
    assert parent("chat judge") == "eval.case"
    assert parent("execute_tool get_filings") == "invoke_agent"

    agent = by_name["invoke_agent"].attributes or {}
    assert agent["gen_ai.usage.input_tokens"] == 1200
    assert agent["gen_ai.usage.output_tokens"] == 300
    assert agent["recon.cost_eur"] == pytest.approx(0.07)
    assert agent["gen_ai.request.model"] == "claude-sonnet-5"

    judge = by_name["chat judge"].attributes or {}
    assert judge["gen_ai.request.model"] == "claude-haiku-4-5-20251001"
    assert judge["recon.num_turns"] == 2
    assert judge["recon.cost_eur"] == pytest.approx(0.02)

    run = by_name["eval.run"].attributes or {}
    assert run["recon.agent_cost_eur"] == pytest.approx(0.07)
    assert run["recon.judge_cost_eur"] == pytest.approx(0.02)
    assert str(run["recon.run_id"]).startswith("eval-")


def test_tool_spans_run_back_to_back_and_mark_only_failures(
    monkeypatch: pytest.MonkeyPatch, fresh_exporter: None
) -> None:
    _run_one_case(monkeypatch, _result(TOOLS))
    finished = _finished()
    agent = next(s for s in finished if s.name == "invoke_agent")
    tools = sorted(
        (s for s in finished if s.name.startswith("execute_tool")),
        key=lambda s: s.start_time or 0,
    )
    assert [s.name.removeprefix("execute_tool ") for s in tools] == [
        "get_financial_facts",
        "get_filings",
        "list_companies",
    ]
    assert tools[0].start_time == agent.start_time
    for before, after in pairwise(tools):
        assert after.start_time == before.end_time
    assert [(s.end_time or 0) - (s.start_time or 0) for s in tools] == [
        30_000_000,
        20_000_000,
        10_000_000,
    ]
    assert [s.status.status_code for s in tools] == [
        StatusCode.UNSET,
        StatusCode.ERROR,
        StatusCode.UNSET,
    ]
    assert (tools[1].attributes or {})["error.type"] == "unavailable"
    assert all((s.attributes or {})["recon.timing"] == "sequential" for s in tools)


def test_a_failed_agent_run_marks_its_span_as_an_error(
    monkeypatch: pytest.MonkeyPatch, fresh_exporter: None
) -> None:
    failed = _result([], answer="").model_copy(update={"error": "budget exceeded"})
    _run_one_case(monkeypatch, failed)
    agent = next(s for s in _finished() if s.name == "invoke_agent")
    assert agent.status.status_code == StatusCode.ERROR
    assert (agent.attributes or {})["error.type"] == "agent_error"


def test_an_api_request_is_traced_with_its_request_id(
    monkeypatch: pytest.MonkeyPatch, fresh_exporter: None
) -> None:
    from recon.api import main as api_main

    monkeypatch.setitem(api_main._runtimes, "single", _Runtime(_result(TOOLS[:1])))
    response = TestClient(api_main.app).post(
        "/investigate", json={"question": "q"}, headers={"X-Request-ID": "req-42"}
    )
    assert response.status_code == 200
    finished = _finished()
    request = next(s for s in finished if s.name == "POST /investigate")
    assert (request.attributes or {})["recon.request_id"] == "req-42"
    assert (request.attributes or {})["http.response.status_code"] == 200
    agent = next(s for s in finished if s.name == "invoke_agent")
    assert agent.parent is not None
    assert agent.parent.span_id == request.context.span_id
    assert any(s.name == "execute_tool get_financial_facts" for s in finished)


def test_the_api_starts_and_flushes_tracing_with_its_lifespan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from recon.api import main as api_main

    calls: list[str] = []
    monkeypatch.setattr(
        api_main, "configure_tracing", lambda name: calls.append(f"start {name}")
    )
    monkeypatch.setattr(api_main, "shutdown_tracing", lambda: calls.append("flush"))
    with TestClient(api_main.app):
        assert calls == ["start recon-api"]
    assert calls == ["start recon-api", "flush"]
