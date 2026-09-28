"""Tests for `adapters/sec_edgar.py`: config, User-Agent, rate limiting, retry,
caching and atomic writes. The network is always a `httpx.MockTransport`."""

import json
import os
from datetime import date
from pathlib import Path

import httpx
import pytest

from recon.adapters import sec_edgar

CIK = 1234567
MAIN = f"CIK{CIK:010d}.json"
PAGE = f"CIK{CIK:010d}-submissions-001.json"


class FakeClock:
    """Deterministic clock whose `sleep` advances time instead of waiting."""

    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _edgar(handler, clock: FakeClock | None = None, rps: float = 5.0):
    clock = clock or FakeClock()
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return sec_edgar.EdgarClient(http, rps, sleep=clock.sleep, clock=clock.clock)


def _config() -> sec_edgar.EdgarConfig:
    return sec_edgar.EdgarConfig(
        filed_cutoff=date(2025, 4, 7),
        taxonomies=("us-gaap",),
        forms=("10-K",),
        max_requests_per_second=5.0,
    )


@pytest.mark.unit
def test_repo_config_loads() -> None:
    config = sec_edgar.load_config()
    assert isinstance(config.filed_cutoff, date)
    assert "us-gaap" in config.taxonomies
    assert config.max_requests_per_second <= 10


@pytest.mark.unit
def test_invalid_config_names_what_is_needed(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("filed_cutoff: not-a-date\n", encoding="utf-8")
    with pytest.raises(ValueError, match="filed_cutoff"):
        sec_edgar.load_config(path)


@pytest.mark.unit
@pytest.mark.parametrize("value", [None, "", "no contact here"])
def test_user_agent_required(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv(sec_edgar.USER_AGENT_ENV, raising=False)
    else:
        monkeypatch.setenv(sec_edgar.USER_AGENT_ENV, value)
    with pytest.raises(RuntimeError, match=sec_edgar.USER_AGENT_ENV):
        sec_edgar.user_agent_from_env()


@pytest.mark.unit
def test_user_agent_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(sec_edgar.USER_AGENT_ENV, "Test Person test@example.com")
    assert sec_edgar.user_agent_from_env() == "Test Person test@example.com"


@pytest.mark.unit
def test_client_sends_user_agent() -> None:
    client = sec_edgar.build_client("Test Person test@example.com")
    assert client.headers["User-Agent"] == "Test Person test@example.com"


@pytest.mark.unit
def test_requests_are_spaced_by_the_rate_limit() -> None:
    clock = FakeClock()
    edgar = _edgar(lambda request: httpx.Response(200, json={}), clock, rps=5.0)
    edgar.get("https://example.test/a")
    edgar.get("https://example.test/b")
    assert clock.sleeps == [pytest.approx(0.2)]


@pytest.mark.unit
def test_retries_transient_status_then_succeeds() -> None:
    responses = iter([httpx.Response(429), httpx.Response(200, json={"ok": True})])
    clock = FakeClock()
    edgar = _edgar(lambda request: next(responses), clock)
    response = edgar.get("https://example.test/x")
    assert response is not None and response.json() == {"ok": True}
    assert sec_edgar._BASE_BACKOFF_S in clock.sleeps


@pytest.mark.unit
def test_retry_honors_retry_after() -> None:
    responses = iter(
        [httpx.Response(503, headers={"Retry-After": "7"}), httpx.Response(200)]
    )
    clock = FakeClock()
    _edgar(lambda request: next(responses), clock).get("https://example.test/x")
    assert 7.0 in clock.sleeps


@pytest.mark.unit
def test_gives_up_after_max_attempts() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(503)

    with pytest.raises(RuntimeError, match="kept returning 503"):
        _edgar(handler).get("https://example.test/x")
    assert len(calls) == sec_edgar._MAX_ATTEMPTS


@pytest.mark.unit
def test_not_found_returns_none() -> None:
    assert _edgar(lambda request: httpx.Response(404)).get("https://x.test") is None


@pytest.mark.unit
def test_client_error_is_raised_not_retried() -> None:
    with pytest.raises(httpx.HTTPStatusError):
        _edgar(lambda request: httpx.Response(403)).get("https://example.test/x")


def _sec_handler(requested: list[str], *, facts_status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        requested.append(name)
        if request.url.path.startswith("/submissions/") and name == MAIN:
            return httpx.Response(
                200,
                json={"filings": {"recent": {}, "files": [{"name": PAGE}]}},
            )
        if name == PAGE:
            return httpx.Response(200, json={"accessionNumber": []})
        if "/companyfacts/" in request.url.path:
            return httpx.Response(facts_status, json={"facts": {}})
        return httpx.Response(404)

    return handler


@pytest.mark.unit
def test_fetch_company_gets_submissions_pages_and_facts(tmp_path: Path) -> None:
    requested: list[str] = []
    result = sec_edgar.fetch_company(_edgar(_sec_handler(requested)), CIK, tmp_path)

    assert result == {"submissions": [MAIN, PAGE], "companyfacts": True}
    assert (tmp_path / "submissions" / MAIN).exists()
    assert (tmp_path / "submissions" / PAGE).exists()
    assert (tmp_path / "companyfacts" / MAIN).exists()


@pytest.mark.unit
def test_fetch_company_resumes_from_cache(tmp_path: Path) -> None:
    requested: list[str] = []
    edgar = _edgar(_sec_handler(requested))
    sec_edgar.fetch_company(edgar, CIK, tmp_path)
    requested.clear()
    sec_edgar.fetch_company(edgar, CIK, tmp_path)
    assert requested == []


@pytest.mark.unit
def test_fetch_company_without_xbrl_facts(tmp_path: Path) -> None:
    requested: list[str] = []
    edgar = _edgar(_sec_handler(requested, facts_status=404))
    result = sec_edgar.fetch_company(edgar, CIK, tmp_path)
    assert result["companyfacts"] is False
    assert not (tmp_path / "companyfacts" / MAIN).exists()


@pytest.mark.unit
def test_atomic_write_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_replace(src: str, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", failing_replace)
    dest = tmp_path / "out.json"
    with pytest.raises(OSError, match="disk full"):
        sec_edgar.atomic_write(dest, b"{}")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.unit
def test_manifest_records_cutoff_and_hashes(tmp_path: Path) -> None:
    (tmp_path / "companyfacts").mkdir()
    (tmp_path / "companyfacts" / MAIN).write_text("{}", encoding="utf-8")
    path = sec_edgar.write_manifest(tmp_path, "20260928", _config(), {"EXWD": {}})

    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["snapshot_id"] == "20260928"
    assert manifest["filed_cutoff"] == "2025-04-07"
    assert list(manifest["sha256"]) == [f"companyfacts/{MAIN}"]


@pytest.mark.unit
def test_snapshot_id_is_the_fetch_date() -> None:
    assert sec_edgar.snapshot_id(date(2026, 9, 28)) == "20260928"


@pytest.mark.unit
def test_fetch_company_tickers_is_cached_per_day(tmp_path: Path) -> None:
    """A day-scoped cache path, not a single fixed file, so a later run on a
    new day re-fetches instead of silently reusing a stale ticker/CIK map."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json={})

    edgar = _edgar(handler)

    dest_day1 = sec_edgar.fetch_company_tickers(edgar, tmp_path, date(2026, 9, 28))
    dest_day2 = sec_edgar.fetch_company_tickers(edgar, tmp_path, date(2026, 9, 29))

    assert dest_day1 != dest_day2
    assert dest_day1 == tmp_path / "20260928" / "company_tickers.json"
    assert requested == [sec_edgar.TICKERS_URL, sec_edgar.TICKERS_URL]


@pytest.mark.unit
def test_fetch_company_tickers_reuses_same_day_cache(tmp_path: Path) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json={})

    edgar = _edgar(handler)
    today = date(2026, 9, 28)

    sec_edgar.fetch_company_tickers(edgar, tmp_path, today)
    sec_edgar.fetch_company_tickers(edgar, tmp_path, today)

    assert requested == [sec_edgar.TICKERS_URL]


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    [
        "../../escape.json",
        "sub/CIK0001234567-submissions-001.json",
        "CIK0001234567-submissions-001.json/../x.json",
        "CIK9999999999-submissions-001.json",  # another company's page
        "CIK0001234567-submissions-001.txt",
    ],
)
def test_unexpected_submissions_page_names_are_refused(
    tmp_path: Path, name: str
) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(
            200, json={"filings": {"recent": {}, "files": [{"name": name}]}}
        )

    with pytest.raises(ValueError, match="unexpected submissions page"):
        sec_edgar.fetch_company(_edgar(handler), CIK, tmp_path)
    assert len(requested) == 1  # only the main file; the bad page is never requested
    assert sorted(p.name for p in tmp_path.rglob("*") if p.is_file()) == [MAIN]


@pytest.mark.unit
def test_submissions_page_path_accepts_sec_naming(tmp_path: Path) -> None:
    assert sec_edgar.submissions_page_path(tmp_path, CIK, PAGE) == (
        tmp_path / "submissions" / PAGE
    )
