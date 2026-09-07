"""Tests for `api/main.py`. `docs/contracts.md` section 5.

`httpx.AsyncClient` against the app in-process via `ASGITransport` — no real
server, no real agent run: `_runtime.run` and the two `health.py` checks are
monkeypatched, so nothing here calls a model or a network service.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from recon.api import main as api_main
from recon.contracts import AgentResult, Case

pytestmark = [pytest.mark.unit, pytest.mark.anyio]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


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

    def fake_run(case: Case) -> AgentResult:
        captured["case"] = case
        return _agent_result(case.case_id)

    monkeypatch.setattr(api_main._runtime, "run", fake_run)

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
    monkeypatch.setattr(
        api_main._runtime, "run", lambda case: _agent_result(case.case_id)
    )

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


async def test_investigate_empty_question_is_422() -> None:
    async with await _client() as client:
        resp = await client.post("/investigate", json={"question": "   "})

    assert resp.status_code == 422
    assert resp.json()["field"] == "question"


async def test_investigate_unsupported_mode_is_422() -> None:
    async with await _client() as client:
        resp = await client.post(
            "/investigate", json={"question": "q", "mode": "multi"}
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


async def test_investigate_timeout_is_504(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    def slow_run(case: Case) -> AgentResult:
        time.sleep(0.2)
        return _agent_result(case.case_id)

    monkeypatch.setattr(api_main._runtime, "run", slow_run)
    monkeypatch.setattr(api_main, "REQUEST_TIMEOUT_S", 0.01)

    async with await _client() as client:
        resp = await client.post("/investigate", json={"question": "q"})

    assert resp.status_code == 504


async def test_metrics_returns_prometheus_text() -> None:
    async with await _client() as client:
        resp = await client.get("/metrics")

    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert "recon_api_requests_total" in resp.text


async def test_docs_renders() -> None:
    async with await _client() as client:
        resp = await client.get("/docs")

    assert resp.status_code == 200
