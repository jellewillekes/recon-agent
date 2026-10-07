"""Tests for `api/main.py`. `docs/contracts.md` section 5.

`httpx.AsyncClient` against the app in-process via `ASGITransport` — no real
server, no real agent run: `_runtimes[mode].run_async` (what `investigate()` actually
awaits) and the two `health.py` checks are monkeypatched, so nothing here
calls a model or a network service.
"""

import asyncio
import re
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from recon.api import main as api_main
from recon.contracts import AgentResult, Case
from recon.runtimes.run_budget import budget_section

pytestmark = [pytest.mark.unit, pytest.mark.anyio]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _no_real_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test that doesn't patch a mode's runtime fails instead of running a
    real, paid agent run."""

    async def refuse(case: Case) -> AgentResult:
        raise AssertionError("an API test reached a real runtime; patch run_async")

    for runtime in api_main._runtimes.values():
        monkeypatch.setattr(runtime, "run_async", refuse)


def _agent_result(case_id: str, **overrides: object) -> AgentResult:
    defaults: dict[str, object] = {
        "case_id": case_id,
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


async def _client() -> AsyncClient:
    transport = ASGITransport(app=api_main.app)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_healthz_always_ok() -> None:
    async with await _client() as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_readyz_ok_when_both_checks_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ok_mcp(timeout_s: float) -> tuple[bool, str]:
        return True, "ok"

    async def ok_pg(database_url: str | None, timeout_s: float) -> tuple[bool, str]:
        return True, "ok"

    monkeypatch.setattr(api_main, "check_mcp_server", ok_mcp)
    monkeypatch.setattr(api_main, "check_postgres", ok_pg)

    async with await _client() as client:
        resp = await client.get("/readyz")

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "checks": {"mcp_server": "ok", "postgres": "ok"},
    }


async def test_readyz_503_when_postgres_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def ok_mcp(timeout_s: float) -> tuple[bool, str]:
        return True, "ok"

    async def failing_pg(
        database_url: str | None, timeout_s: float
    ) -> tuple[bool, str]:
        return False, "DATABASE_URL not configured"

    monkeypatch.setattr(api_main, "check_mcp_server", ok_mcp)
    monkeypatch.setattr(api_main, "check_postgres", failing_pg)

    async with await _client() as client:
        resp = await client.get("/readyz")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["postgres"] == "DATABASE_URL not configured"


async def test_investigate_success_echoes_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Case] = {}

    async def fake_run_async(case: Case) -> AgentResult:
        captured["case"] = case
        return _agent_result(case.case_id)

    monkeypatch.setattr(api_main._runtimes["single"], "run_async", fake_run_async)

    async with await _client() as client:
        resp = await client.post(
            "/investigate",
            json={"question": "What is the revenue trend?", "context": {}},
            headers={"X-Request-ID": "my-request-id"},
        )

    assert resp.status_code == 200
    assert resp.headers["X-Request-ID"] == "my-request-id"
    assert resp.json()["case_id"] == "my-request-id"
    assert captured["case"].question == "What is the revenue trend?"
    assert captured["case"].expected_answer == ""


async def test_investigate_generates_request_id_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_async(case: Case) -> AgentResult:
        return _agent_result(case.case_id)

    monkeypatch.setattr(api_main._runtimes["single"], "run_async", fake_run_async)

    async with await _client() as client:
        resp = await client.post("/investigate", json={"question": "q"})

    assert resp.status_code == 200
    assert resp.headers["X-Request-ID"]
    assert resp.json()["case_id"] == resp.headers["X-Request-ID"]


async def test_investigate_missing_question_is_400() -> None:
    async with await _client() as client:
        resp = await client.post("/investigate", json={"context": {}})

    assert resp.status_code == 400
    assert "question" in resp.json()["fields"]


async def test_investigate_400_is_counted_in_metrics() -> None:
    async with await _client() as client:
        await client.post("/investigate", json={"context": {}})
        resp = await client.get("/metrics")

    assert re.search(
        r'recon_api_requests_total\{route="/investigate",status="400"\} [1-9]',
        resp.text,
    )


async def test_investigate_empty_question_is_422() -> None:
    async with await _client() as client:
        resp = await client.post("/investigate", json={"question": "   "})

    assert resp.status_code == 422
    assert resp.json()["field"] == "question"


async def test_investigate_unsupported_mode_is_422() -> None:
    async with await _client() as client:
        resp = await client.post(
            "/investigate", json={"question": "q", "mode": "swarm"}
        )

    assert resp.status_code == 422
    assert resp.json()["field"] == "mode"


async def test_investigate_unsupported_runtime_is_422() -> None:
    async with await _client() as client:
        resp = await client.post(
            "/investigate", json={"question": "q", "runtime": "langgraph"}
        )

    assert resp.status_code == 422
    assert resp.json()["field"] == "runtime"


async def test_investigate_timeout_is_504(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import asyncio

    cleanup_ran = []

    async def hanging_run_async(case: Case) -> AgentResult:
        try:
            await asyncio.Event().wait()
            return _agent_result(case.case_id)  # pragma: no cover - unreachable
        finally:
            cleanup_ran.append(True)

    monkeypatch.setattr(api_main._runtimes["single"], "run_async", hanging_run_async)
    monkeypatch.setattr(api_main, "REQUEST_TIMEOUT_S", 0.01)

    with caplog.at_level("WARNING", logger="recon.api.main"):
        async with await _client() as client:
            resp = await client.post("/investigate", json={"question": "q"})

    assert resp.status_code == 504
    # run_async is a real coroutine, so wait_for's cancellation actually
    # reaches it (see docs/adr/0006-api-timeout-cancellation-fixed.md) -
    # unlike the old run()-in-a-thread bridge, cleanup genuinely runs before
    # this response goes out, not just the warning log.
    assert cleanup_ran == [True]
    assert "timed out" in caplog.text


async def test_metrics_returns_prometheus_text() -> None:
    async with await _client() as client:
        resp = await client.get("/metrics")

    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert "recon_api_requests_total" in resp.text


async def test_root_serves_local_research_ui() -> None:
    async with await _client() as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Financial research." in resp.text
    assert "Evidence you can inspect." in resp.text
    # The data-source notice is filled from /capabilities, not hardcoded.
    assert 'id="data-note-text"' in resp.text
    # Research and evaluation are separate, labelled views (#117).
    assert 'id="research-view"' in resp.text
    assert 'id="evaluation-view"' in resp.text
    assert "Benchmark results · not live answers" in resp.text


async def test_ui_assets_are_served_and_submit_to_investigate() -> None:
    async with await _client() as client:
        css = await client.get("/styles.css")
        javascript = await client.get("/app.js")
        history = await client.get("/history.js")
        evals = await client.get("/evals.js")

    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    assert javascript.status_code == 200
    assert "javascript" in javascript.headers["content-type"]
    assert 'form.addEventListener("submit"' in javascript.text
    assert 'fetch("/investigate"' in javascript.text
    assert (
        "JSON.stringify({ question: value, context: {}, mode: modeSelect.value })"
        in javascript.text
    )
    assert 'fetch("/capabilities")' in javascript.text
    assert "await response.json()" in javascript.text
    assert "renderResult(payload" in javascript.text
    assert 'fetch("/runs?limit=20")' in history.text
    assert 'fetch("/evals")' in evals.text
    assert 'fetch("/evals/compare?" + params)' in evals.text
    # #116: each case's failure class and run-path breakdown, and the counts.
    assert "score.failure_class" in evals.text
    assert "score.trajectory" in evals.text
    assert "renderFailureCounts(run.aggregate)" in evals.text
    # #122 review: run history still loads when /capabilities can't be read.
    assert "loadHistoryWithoutCapabilities()" in javascript.text
    assert "function loadHistoryWithoutCapabilities()" in history.text
    assert "Enter a research question before starting." in javascript.text
    for script in (javascript, history, evals):
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in script.text


async def test_docs_renders() -> None:
    async with await _client() as client:
        resp = await client.get("/docs")

    assert resp.status_code == 200


@pytest.mark.unit
def test_the_run_budget_ends_before_every_api_request_timeout() -> None:
    """A run that hits its wall-clock budget returns a partial AgentResult. If
    the API's request timeout came first, the caller would get a bare 504."""
    root = Path(__file__).resolve().parent.parent
    models = yaml.safe_load((root / "config" / "models.yaml").read_text())
    # Every mode's budget, so a slower mode can't outlast the request.
    wall_clock = max(
        float(budget_section(models, mode)["max_wall_clock_s"])
        for mode in ("single", "multi")
    )
    chart = yaml.safe_load((root / "charts/recon-agent/values.yaml").read_text())
    compose = yaml.safe_load((root / "docker/compose.yaml").read_text())
    timeouts = {
        "api default": api_main.REQUEST_TIMEOUT_S,
        "chart": float(chart["env"]["requestTimeoutS"]),
        "compose": float(
            compose["services"]["api"]["environment"]["RECON_API_REQUEST_TIMEOUT_S"]
        ),
    }
    for where, timeout in timeouts.items():
        assert wall_clock < timeout, where


@pytest.mark.unit
def test_the_service_refuses_to_start_with_an_exported_sdk_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every request would bill the API instead of the subscription."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "would-bill-the-api")

    async def start() -> None:
        async with api_main._lifespan(api_main.app):
            pass

    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        asyncio.run(start())
