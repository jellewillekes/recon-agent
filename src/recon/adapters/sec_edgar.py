"""Fetch SEC EDGAR data into a local, snapshot-scoped cache.

Downloads `company_tickers.json`, `submissions/CIK##########.json` (plus its
older pages) and `api/xbrl/companyfacts/CIK##########.json` for the companies
listed in a reviewed tickers file. Nothing here interprets the data - see
`sec_edgar_normalize.py` for the Parquet tables, and
`docs/adr/0015-sec-edgar-fact-derivation.md` for the rules.

SEC's fair-access policy requires a contact User-Agent on every request and at
most 10 requests/second. The User-Agent only ever comes from
`SEC_EDGAR_USER_AGENT`, never from a committed file.
"""

import hashlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

logger = logging.getLogger(__name__)

USER_AGENT_ENV = "SEC_EDGAR_USER_AGENT"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/{name}"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

DEFAULT_CONFIG_PATH = Path("config/sec_edgar.yaml")
DEFAULT_RAW_DIR = Path("data/raw/sec_edgar")
DEFAULT_PROCESSED_DIR = Path("data/processed/sec_edgar")
TICKERS_FILENAME = "tickers.txt"
MANIFEST_FILENAME = "manifest.json"

_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 4
_BASE_BACKOFF_S = 1.0


@dataclass(frozen=True)
class EdgarConfig:
    """`config/sec_edgar.yaml`, validated."""

    filed_cutoff: date
    taxonomies: tuple[str, ...]
    forms: tuple[str, ...]
    max_requests_per_second: float


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> EdgarConfig:
    """Read and validate the EDGAR config file."""
    with path.open(encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f)
    try:
        return EdgarConfig(
            filed_cutoff=date.fromisoformat(str(raw["filed_cutoff"])),
            taxonomies=tuple(raw["taxonomies"]),
            forms=tuple(raw["forms"]),
            max_requests_per_second=float(raw["max_requests_per_second"]),
        )
    except (KeyError, ValueError) as exc:
        raise ValueError(
            f"{path} is invalid: {exc!r}. It needs filed_cutoff (YYYY-MM-DD), "
            "taxonomies, forms and max_requests_per_second."
        ) from exc


def user_agent_from_env() -> str:
    """The contact User-Agent SEC requires, from the environment only."""
    value = os.environ.get(USER_AGENT_ENV, "").strip()
    if "@" not in value:
        raise RuntimeError(
            f"{USER_AGENT_ENV} is not set to a contact User-Agent. SEC's fair-access "
            "policy requires one on every request. Set it to e.g. "
            "'Your Name your.email@example.com' and retry."
        )
    return value


class EdgarClient:
    """Rate-limited GET with retry on SEC's transient statuses."""

    def __init__(
        self,
        client: httpx.Client,
        max_requests_per_second: float,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._min_interval = 1.0 / max_requests_per_second
        self._sleep = sleep
        self._clock = clock
        self._last_request: float | None = None

    def _wait_turn(self) -> None:
        if self._last_request is not None:
            wait = self._min_interval - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._clock()

    def get(self, url: str) -> httpx.Response | None:
        """GET `url`. Returns `None` on 404, raises after exhausting retries."""
        for attempt in range(_MAX_ATTEMPTS):
            self._wait_turn()
            response = self._client.get(url, timeout=30)
            if response.status_code == 404:
                return None
            if response.status_code not in _RETRY_STATUSES:
                response.raise_for_status()
                return response
            if attempt < _MAX_ATTEMPTS - 1:
                retry_after = response.headers.get("Retry-After", "")
                delay = (
                    float(retry_after)
                    if retry_after.isdigit()
                    else _BASE_BACKOFF_S * 2**attempt
                )
                logger.warning(
                    "%s returned %s, retrying in %.1fs",
                    url,
                    response.status_code,
                    delay,
                )
                self._sleep(delay)
        raise RuntimeError(
            f"{url} kept returning {response.status_code} after {_MAX_ATTEMPTS} "
            "attempts. SEC may be throttling this client. Wait a few minutes and "
            "rerun: files already fetched are kept."
        )


def build_client(user_agent: str) -> httpx.Client:
    """An `httpx.Client` carrying SEC's required headers."""
    return httpx.Client(
        headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
        follow_redirects=True,
    )


def atomic_write(dest: Path, content: bytes) -> None:
    """Write via a temp file and rename, so a killed run never leaves a
    partial file that a later run would treat as cached."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent)
    try:
        with os.fdopen(fd, "wb") as tmp_file:
            tmp_file.write(content)
        os.replace(tmp_name, dest)
    except BaseException:
        os.unlink(tmp_name)
        raise


def fetch_to(edgar: EdgarClient, url: str, dest: Path) -> bool:
    """Download `url` to `dest` unless cached. False when SEC has no such file."""
    if dest.exists():
        return True
    response = edgar.get(url)
    if response is None:
        return False
    atomic_write(dest, response.content)
    return True


def fetch_company_tickers(
    edgar: EdgarClient, raw_dir: Path, today: date | None = None
) -> Path:
    """SEC's ticker -> CIK map, cached per day like every other snapshot.

    Scoping the cache to `snapshot_id()` (rather than a single fixed path)
    means a stale map from a previous day is never silently reused: each new
    day's `--from-dataset` run re-fetches it, picking up new IPOs and ticker
    changes instead of matching against a map that can be arbitrarily old.
    """
    dest = raw_dir / snapshot_id(today) / "company_tickers.json"
    if not fetch_to(edgar, TICKERS_URL, dest):
        raise RuntimeError(f"{TICKERS_URL} returned 404. Check the URL in SEC's docs.")
    return dest


def _fetch_submissions(edgar: EdgarClient, cik: int, snapshot_dir: Path) -> list[str]:
    """The main submissions file plus the older pages it lists."""
    main_name = f"CIK{cik:010d}.json"
    main_dest = snapshot_dir / "submissions" / main_name
    if not fetch_to(edgar, SUBMISSIONS_URL.format(name=main_name), main_dest):
        return []
    fetched = [main_name]
    payload = json.loads(main_dest.read_bytes())
    for page in payload.get("filings", {}).get("files", []):
        name = page["name"]
        dest = snapshot_dir / "submissions" / name
        if fetch_to(edgar, SUBMISSIONS_URL.format(name=name), dest):
            fetched.append(name)
    return fetched


def fetch_company(edgar: EdgarClient, cik: int, snapshot_dir: Path) -> dict[str, Any]:
    """Fetch one company's submissions and companyfacts into `snapshot_dir`."""
    submissions = _fetch_submissions(edgar, cik, snapshot_dir)
    facts_dest = snapshot_dir / "companyfacts" / f"CIK{cik:010d}.json"
    has_facts = fetch_to(edgar, COMPANYFACTS_URL.format(cik=cik), facts_dest)
    if not has_facts:
        logger.warning("CIK %s has no companyfacts (no XBRL financial data).", cik)
    return {"submissions": submissions, "companyfacts": has_facts}


def snapshot_id(today: date | None = None) -> str:
    """Snapshot ids are the fetch date. A same-day rerun resumes the snapshot."""
    return (today or datetime.now(UTC).date()).strftime("%Y%m%d")


def write_manifest(
    snapshot_dir: Path,
    snapshot: str,
    config: EdgarConfig,
    companies: dict[str, dict[str, Any]],
) -> Path:
    """Record what this snapshot contains, with a sha256 per cached file."""
    hashes = {
        str(path.relative_to(snapshot_dir)): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(snapshot_dir.rglob("*.json"))
        if path.name != MANIFEST_FILENAME
    }
    manifest = {
        "snapshot_id": snapshot,
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "filed_cutoff": config.filed_cutoff.isoformat(),
        "companies": companies,
        "sha256": hashes,
    }
    dest = snapshot_dir / MANIFEST_FILENAME
    atomic_write(dest, json.dumps(manifest, indent=2).encode("utf-8"))
    return dest
