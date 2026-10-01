"""Fetch filing text from SEC EDGAR for the knowledge corpus (step 13, #18).

XBRL facts carry numbers, not the narrative behind them. About half the
benchmark's questions need text: earnings releases for beat-or-miss and
adjustments, and 10-K sections for qualitative questions. This fetches, for
each company in the reviewed tickers files:

- the EX-99.1 exhibit of 8-Ks reporting results (item 2.02), and
- the MD&A (items 7 and 7A) and risk factors (item 1A) of 10-Ks,

filed in the window before `filed_cutoff`, so the agent can't read a filing
made after a question was asked. Files are cached under the dated raw
snapshot like the rest of `sec_edgar.py`. See docs/adr/0025-retrieval-over-filing-text.md.
"""

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from recon.adapters.sec_edgar import EdgarClient, fetch_to

ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{name}"
WINDOW = timedelta(days=730)
EARNINGS_ITEM = "2.02"
_EX_99_1 = re.compile(r"^EX-99(\.0?1)?$", re.IGNORECASE)
_MAX_TEXT_CHARS = 400_000


@dataclass(frozen=True)
class Filing:
    """One filing whose text goes into the corpus."""

    cik: int
    accession: str
    form: str
    filed: date
    primary_document: str

    @property
    def folder(self) -> str:
        return self.accession.replace("-", "")


def select_filings(submissions: dict[str, Any], cutoff: date) -> list[Filing]:
    """Results 8-Ks and 10-Ks filed in the window up to `cutoff`, newest first."""
    recent = submissions.get("filings", {}).get("recent", {})
    cik = int(submissions["cik"])
    picked = []
    for accession, form, filed, items, primary in zip(
        recent.get("accessionNumber", []),
        recent.get("form", []),
        recent.get("filingDate", []),
        recent.get("items", []),
        recent.get("primaryDocument", []),
        strict=True,
    ):
        day = date.fromisoformat(filed)
        if not cutoff - WINDOW <= day <= cutoff:
            continue
        if form == "10-K" or (form == "8-K" and EARNINGS_ITEM in items.split(",")):
            picked.append(Filing(cik, accession, form, day, primary))
    return sorted(picked, key=lambda f: f.filed, reverse=True)


class _IndexTable(HTMLParser):
    """Rows of a filing's `-index.htm` document table: (href, type)."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[tuple[str, str]] = []
        self._cells: list[str] = []
        self._href = ""
        self._in_cell = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._cells, self._href = [], ""
        elif tag == "td":
            self._in_cell = True
            self._cells.append("")
        elif tag == "a" and self._in_cell and not self._href:
            self._href = dict(attrs).get("href") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "td":
            self._in_cell = False
        elif tag == "tr" and len(self._cells) >= 4 and self._href:
            self.rows.append((self._href, self._cells[3].strip()))

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cells[-1] += data


def exhibit_99_1(index_html: str) -> str | None:
    """The file name of a filing's EX-99.1 exhibit, from its index page."""
    table = _IndexTable()
    table.feed(index_html)
    for href, doc_type in table.rows:
        if _EX_99_1.match(doc_type):
            return href.rsplit("/", 1)[-1]
    return None


def filing_dir(snapshot_dir: Path, filing: Filing) -> Path:
    """Where a filing's documents are cached in a raw snapshot."""
    return snapshot_dir / "filings" / f"CIK{filing.cik:010d}" / filing.folder


def fetch_filing(edgar: EdgarClient, filing: Filing, snapshot_dir: Path) -> Path | None:
    """Download the document whose text goes into the corpus. None if SEC has none."""
    folder = filing_dir(snapshot_dir, filing)
    if filing.form == "10-K":
        name = filing.primary_document
    else:
        index_name = f"{filing.accession}-index.htm"
        index_path = folder / index_name
        url = ARCHIVES_URL.format(cik=filing.cik, folder=filing.folder, name=index_name)
        if not fetch_to(edgar, url, index_path):
            return None
        found = exhibit_99_1(index_path.read_text(encoding="utf-8", errors="replace"))
        if found is None:
            return None
        name = found
    dest = folder / name
    url = ARCHIVES_URL.format(cik=filing.cik, folder=filing.folder, name=name)
    return dest if fetch_to(edgar, url, dest) else None


class _TextExtractor(HTMLParser):
    """Visible text of an HTML filing, one block element per line."""

    _BLOCKS = frozenset({"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table"})
    _SKIP = frozenset({"script", "style", "head", "title"})

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP or tag.startswith("ix:header"):
            self._skipping += 1
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if (tag in self._SKIP or tag.startswith("ix:header")) and self._skipping:
            self._skipping -= 1
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Readable text from filing HTML: tags, scripts and the inline-XBRL header dropped."""
    extractor = _TextExtractor()
    extractor.feed(html)
    lines = (" ".join(line.split()) for line in "".join(extractor.parts).splitlines())
    return "\n".join(line for line in lines if line)


_ITEM_HEADING = re.compile(
    r"^item\s+(1a|1b|1c|2|7a|7|8)\s*[.:\-—–]?\s*(.*)$", re.IGNORECASE | re.MULTILINE
)
_MAX_HEADING_CHARS = 120
_KEEP = {"1a": "risk factors", "7": "md&a", "7a": "market risk"}


def tenk_sections(text: str) -> dict[str, str]:
    """Item 1A, 7 and 7A of a 10-K's text, keyed by section name.

    Headings appear twice, in the table of contents and in the body. The
    body's is the last occurrence, so each section runs from the last heading
    of its item to the next item heading after it. Missing items are left out.
    """
    starts: dict[str, int] = {}
    for match in _ITEM_HEADING.finditer(text):
        # A heading is a short line. A body sentence that happens to start
        # with "Item 7 ..." runs on, and mustn't move the section start.
        if len(match.group(0)) <= _MAX_HEADING_CHARS:
            starts[match.group(1).lower()] = match.start()
    ordered = sorted(starts.items(), key=lambda item: item[1])
    sections = {}
    for i, (item, start) in enumerate(ordered):
        if item in _KEEP:
            end = ordered[i + 1][1] if i + 1 < len(ordered) else len(text)
            body = text[start:end].strip()
            if len(body) > 200:
                sections[_KEEP[item]] = body[:_MAX_TEXT_CHARS]
    return sections


def read_submissions(snapshot_dir: Path) -> list[dict[str, Any]]:
    """The main submissions file of every company in a raw snapshot."""
    paths = sorted((snapshot_dir / "submissions").glob("CIK??????????.json"))
    return [json.loads(path.read_bytes()) for path in paths]
