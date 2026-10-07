"""Tests for the research API added in #115: multi mode, `/capabilities`, the
run store and its endpoints. `docs/contracts.md` section 5.

The runtimes are monkeypatched and Postgres is an in-memory fake, so nothing
here calls a model or a network service.
"""

import json
from datetime import datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from recon.api import main as api_main
from recon.api import run_store
from recon.contracts import AgentResult, Case, Claim, Evidence

pytestmark = [pytest.mark.unit, pytest.mark.anyio]

SECRET_URL = "postgresql://recon:hunter2-secret@db.internal:5432/recon"


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


class _FakeConnection:
    """The run store's queries against a dict keyed on run_id, like the real
    table's primary key."""

    def __init__(self, store: dict[str, dict[str, Any]]) -> None:
        self._store = store

    async def execute(self, sql: str, *params: Any) -> None:
        if "CREATE TABLE" in sql:
            return
        assert "INSERT INTO research_runs" in sql
        run_id, created_at, _question, _mode, run = params
        assert isinstance(created_at, datetime)
        self._store.setdefault(
            run_id,
            {"run_id": run_id, "created_at": created_at, "run": run, "feedback": None},
        )

    async def fetch(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        limit, offset = params
        rows = sorted(self._store.values(), key=lambda r: r["created_at"], reverse=True)
        return rows[offset : offset + limit]

    async def fetchrow(self, sql: str, *params: Any) -> dict[str, Any] | None:
        if sql.lstrip().startswith("UPDATE"):
            run_id, feedback = params
            if run_id not in self._store:
                return None
            self._store[run_id]["feedback"] = feedback
            return {"run_id": run_id}
        (run_id,) = params
        return self._store.get(run_id)

    async def close(self) -> None:
        pass


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}

    async def fake_connect(database_url: str) -> _FakeConnection:
        assert database_url == SECRET_URL
        return _FakeConnection(rows)

    monkeypatch.setattr(run_store.asyncpg, "connect", fake_connect)
    monkeypatch.setattr(api_main, "DATABASE_URL", SECRET_URL)
    monkeypatch.setattr(api_main, "_data_source", lambda: ("fixture", "fixture-abc"))
    return rows


def _agent_result(case: Case, mode: str = "single") -> AgentResult:
    return AgentResult(
        case_id=case.case_id,
        answer="Revenue was 520.",
        evidence=["[Ea] financial_fact (FIRM-001): revenue 520"],
        confidence="high",
        tool_calls=[],
        runtime="agent_sdk",
        mode=mode,  # type: ignore[arg-type]
        tokens_in=10,
        tokens_out=5,
        cost_eur=0.01,
        elapsed_ms=100,
        error=None,
        claims=[Claim(text="Revenue was 520.", importance="key", evidence_refs=["Ea"])],
        evidence_items=[
            Evidence(ref="Ea", verified=True, source_type="financial_fact")
        ],
    )


def _patch_runtimes(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Each mode's runtime answers, and records which mode ran."""
    ran: dict[str, str] = {}
    for mode in ("single", "multi"):

        async def fake_run_async(case: Case, mode: str = mode) -> AgentResult:
            ran["mode"] = mode
            return _agent_result(case, mode)

        monkeypatch.setattr(api_main._runtimes[mode], "run_async", fake_run_async)
    return ran


async def _client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=api_main.app), base_url="http://test"
    )


async def test_investigate_runs_multi_mode_when_asked(
    monkeypatch: pytest.MonkeyPatch, store: dict[str, dict[str, Any]]
) -> None:
    ran = _patch_runtimes(monkeypatch)
    async with await _client() as client:
        resp = await client.post(
            "/investigate", json={"question": "q", "mode": "multi"}
        )

    assert resp.status_code == 200
    assert ran["mode"] == "multi"
    assert resp.json()["mode"] == "multi"


async def test_capabilities_list_what_this_build_supports(
    store: dict[str, dict[str, Any]],
) -> None:
    async with await _client() as client:
        body = (await client.get("/capabilities")).json()

    runtimes = {r["name"]: r for r in body["runtimes"]}
    assert runtimes["agent_sdk"]["supported"] is True
    assert runtimes["agent_sdk"]["modes"] == ["single", "multi"]
    assert runtimes["langgraph"]["supported"] is False
    assert "API key" in runtimes["langgraph"]["reason"]
    assert body["default_mode"] == "single"
    assert body["data_source"] == {"kind": "fixture", "snapshot": "fixture-abc"}
    assert body["run_history"] is True


async def test_capabilities_say_when_run_history_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_main, "DATABASE_URL", None)
    monkeypatch.setattr(api_main, "_data_source", lambda: ("fixture", "fixture-abc"))
    async with await _client() as client:
        body = (await client.get("/capabilities")).json()

    assert body["run_history"] is False


async def test_a_finished_run_is_saved_and_can_be_reopened(
    monkeypatch: pytest.MonkeyPatch, store: dict[str, dict[str, Any]]
) -> None:
    _patch_runtimes(monkeypatch)
    async with await _client() as client:
        await client.post(
            "/investigate",
            json={"question": "What was revenue?", "context": {"k": "v"}},
            headers={"X-Request-ID": "run-1"},
        )
        listed = (await client.get("/runs")).json()
        run = (await client.get("/runs/run-1")).json()

    summary = listed["runs"][0]
    assert summary["run_id"] == "run-1"
    assert summary["question"] == "What was revenue?"
    assert summary["claim_count"] == 1
    assert summary["verified_evidence_count"] == 1
    assert run["result"]["claims"][0]["evidence_refs"] == ["Ea"]
    assert run["context"] == {"k": "v"}
    assert run["data_source"] == "fixture-abc"
    assert run["feedback"] is None


async def test_runs_are_listed_newest_first_with_paging(
    monkeypatch: pytest.MonkeyPatch, store: dict[str, dict[str, Any]]
) -> None:
    _patch_runtimes(monkeypatch)
    async with await _client() as client:
        for run_id in ("a", "b", "c"):
            await client.post(
                "/investigate",
                json={"question": run_id},
                headers={"X-Request-ID": run_id},
            )
        page = (await client.get("/runs", params={"limit": 2, "offset": 1})).json()

    assert [r["run_id"] for r in page["runs"]] == ["b", "a"]
    assert (page["limit"], page["offset"]) == (2, 1)


async def test_an_unknown_run_is_404(store: dict[str, dict[str, Any]]) -> None:
    async with await _client() as client:
        resp = await client.get("/runs/nope")

    assert resp.status_code == 404
    assert "nope" in resp.json()["detail"]


async def test_export_is_a_self_contained_json_download(
    monkeypatch: pytest.MonkeyPatch, store: dict[str, dict[str, Any]]
) -> None:
    _patch_runtimes(monkeypatch)
    async with await _client() as client:
        await client.post(
            "/investigate", json={"question": "q"}, headers={"X-Request-ID": "r1"}
        )
        resp = await client.get("/runs/r1/export")

    assert resp.status_code == 200
    assert 'filename="recon-run-r1.json"' in resp.headers["content-disposition"]
    exported = json.loads(resp.content)
    assert exported["run_id"] == "r1"
    assert exported["result"]["evidence_items"][0]["verified"] is True


async def test_feedback_is_stored_with_the_run(
    monkeypatch: pytest.MonkeyPatch, store: dict[str, dict[str, Any]]
) -> None:
    _patch_runtimes(monkeypatch)
    async with await _client() as client:
        await client.post(
            "/investigate", json={"question": "q"}, headers={"X-Request-ID": "r1"}
        )
        resp = await client.post(
            "/runs/r1/feedback", json={"rating": "down", "note": "wrong year"}
        )
        run = (await client.get("/runs/r1")).json()
        listed = (await client.get("/runs")).json()

    assert resp.status_code == 200
    assert run["feedback"]["rating"] == "down"
    assert run["feedback"]["note"] == "wrong year"
    assert run["feedback"]["created_at"]
    assert listed["runs"][0]["feedback"] == "down"


async def test_feedback_on_an_unknown_run_is_404(
    store: dict[str, dict[str, Any]],
) -> None:
    async with await _client() as client:
        resp = await client.post("/runs/nope/feedback", json={"rating": "up"})

    assert resp.status_code == 404


async def test_feedback_rejects_an_unknown_rating(
    store: dict[str, dict[str, Any]],
) -> None:
    async with await _client() as client:
        resp = await client.post("/runs/r1/feedback", json={"rating": "meh"})

    assert resp.status_code == 400


async def test_run_history_without_postgres_is_503_with_what_to_do(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_main, "DATABASE_URL", None)
    async with await _client() as client:
        resp = await client.get("/runs")

    assert resp.status_code == 503
    assert "DATABASE_URL" in resp.json()["detail"]


async def test_investigate_still_answers_when_saving_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run store outage must not cost the user their answer."""
    _patch_runtimes(monkeypatch)

    async def failing_connect(database_url: str) -> Any:
        raise OSError("connection refused")

    monkeypatch.setattr(run_store.asyncpg, "connect", failing_connect)
    monkeypatch.setattr(api_main, "DATABASE_URL", SECRET_URL)
    monkeypatch.setattr(api_main, "_data_source", lambda: ("fixture", "fixture-abc"))
    async with await _client() as client:
        resp = await client.post("/investigate", json={"question": "q"})

    assert resp.status_code == 200
    assert resp.json()["answer"] == "Revenue was 520."


async def test_no_response_contains_the_database_url_or_its_password(
    monkeypatch: pytest.MonkeyPatch, store: dict[str, dict[str, Any]]
) -> None:
    _patch_runtimes(monkeypatch)
    async with await _client() as client:
        bodies = [
            (
                await client.post(
                    "/investigate",
                    json={"question": "q"},
                    headers={"X-Request-ID": "r1"},
                )
            ).text,
            (await client.get("/capabilities")).text,
            (await client.get("/runs")).text,
            (await client.get("/runs/r1")).text,
            (await client.get("/runs/r1/export")).text,
        ]

    for body in bodies:
        assert "hunter2" not in body
        assert "db.internal" not in body
