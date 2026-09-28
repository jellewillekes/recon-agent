"""Derive which companies the dataset's questions reference, at fetch time.

CLAUDE.md forbids naming companies in committed files, so the list is never
checked in. `derive_tickers` matches each question against SEC's
`company_tickers.json` two ways:

- explicit tickers, e.g. `(EXCHANGE: TICK)` or a bare uppercase token that is
  a known ticker and not a finance acronym
- the company's registered name, normalized, appearing in the question as a
  capitalized phrase

Both are heuristics. The result goes to a gitignored tickers file for review
before anything is fetched, so a wrong or missing match is fixed by hand.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from recon.contracts import Case

_EXCHANGE_TICKER_RE = re.compile(
    r"\((?:NASDAQ|NASDQ|NYSE|AMEX|NYSEAMERICAN)\s*:\s*([A-Z][A-Z.\-]{0,5})\)"
)
_PAREN_TICKER_RE = re.compile(r"\(([A-Z]{1,5})\)")
_BARE_TOKEN_RE = re.compile(r"\b([A-Z]{2,5})\b")

# Uppercase tokens in financial questions that are acronyms, not tickers.
_NOT_TICKERS = frozenset(
    {
        "ARPU", "BPS", "CAGR", "CEO", "CFO", "COGS", "CRM", "EBIT", "EBITDA", "EPS",
        "FCF", "FY", "FYE", "GAAP", "GBV", "GEP", "HCM", "IFP", "KPI", "KPIS", "LTM",
        "NT", "PFS", "PFSS", "SEC", "TTM", "US", "USA", "USD", "YE", "YOY", "YTD",
    }
)  # fmt: skip

# Suffixes stripped from registered names before matching question text.
_NAME_SUFFIXES = frozenset(
    {
        "CO", "COMPANY", "CORP", "CORPORATION", "GROUP", "HOLDINGS", "INC",
        "INCORPORATED", "LTD", "LIMITED", "LLC", "LP", "PLC", "SA", "NV", "AG",
        "THE", "DE", "NEW",
    }
)  # fmt: skip
_MIN_NAME_CHARS = 5
_MAX_NGRAM = 5


@dataclass
class TickerMatch:
    """One company referenced by one or more cases."""

    ticker: str
    cik: int
    title: str
    matched_by: set[str] = field(default_factory=set)
    case_ids: set[str] = field(default_factory=set)


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9]+", text.replace("'", "").replace("’", ""))


def _normalize_name(title: str) -> tuple[str, ...]:
    words = [w.upper() for w in _words(title.replace("&", " AND "))]
    while words and words[-1] in _NAME_SUFFIXES:
        words.pop()
    while words and words[0] == "THE":
        words.pop(0)
    return tuple(words)


def _capitalized_ngrams(question: str) -> set[tuple[str, ...]]:
    """Every run of up to `_MAX_NGRAM` words starting with a capital or digit.

    Requiring the capital keeps a registered name that is also an ordinary
    word from matching lowercase prose.
    """
    words = _words(question)
    grams: set[tuple[str, ...]] = set()
    for i, word in enumerate(words):
        if not (word[0].isupper() or word[0].isdigit()):
            continue
        for n in range(1, _MAX_NGRAM + 1):
            if i + n <= len(words):
                grams.add(tuple(w.upper() for w in words[i : i + n]))
    return grams


def _explicit_tickers(question: str, known: set[str]) -> set[str]:
    found = set(_EXCHANGE_TICKER_RE.findall(question))
    found |= {t for t in _PAREN_TICKER_RE.findall(question) if t in known}
    found |= {
        t
        for t in _BARE_TOKEN_RE.findall(question)
        if t in known and t not in _NOT_TICKERS
    }
    return found


def load_company_tickers(path: Path) -> list[tuple[str, int, str]]:
    """`company_tickers.json` as (ticker, cik, title) rows, in SEC's order."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [
        (str(row["ticker"]), int(row["cik_str"]), str(row["title"]))
        for row in payload.values()
    ]


def derive_tickers(
    cases: list[Case], company_tickers: list[tuple[str, int, str]]
) -> dict[int, TickerMatch]:
    """Companies referenced by `cases`, keyed by CIK."""
    by_ticker = {ticker: (cik, title) for ticker, cik, title in company_tickers}
    by_name: dict[tuple[str, ...], tuple[str, int, str]] = {}
    for ticker, cik, title in company_tickers:
        name = _normalize_name(title)
        if len("".join(name)) >= _MIN_NAME_CHARS:
            by_name.setdefault(name, (ticker, cik, title))

    matches: dict[int, TickerMatch] = {}

    def record(ticker: str, cik: int, title: str, how: str, case_id: str) -> None:
        match = matches.setdefault(
            cik, TickerMatch(ticker=ticker, cik=cik, title=title)
        )
        match.matched_by.add(how)
        match.case_ids.add(case_id)

    for case in cases:
        for ticker in _explicit_tickers(case.question, set(by_ticker)):
            if ticker in by_ticker:
                cik, title = by_ticker[ticker]
                record(ticker, cik, title, "ticker", case.case_id)
        for gram in _capitalized_ngrams(case.question):
            if gram in by_name:
                ticker, cik, title = by_name[gram]
                record(ticker, cik, title, "name", case.case_id)
    return matches


def unmatched_case_ids(cases: list[Case], matches: dict[int, TickerMatch]) -> list[str]:
    """Cases no company was matched for."""
    covered = {case_id for match in matches.values() for case_id in match.case_ids}
    return [case.case_id for case in cases if case.case_id not in covered]


_HEADER = (
    "# Companies to fetch from SEC EDGAR, derived from the dataset's questions.\n"
    "# Review before fetching: delete wrong rows, add missing ones as TICKER<TAB>CIK.\n"
    "# Columns: ticker, cik, matched_by, registered name, case_ids. Only the\n"
    "# first two are read back. This file is gitignored (CLAUDE.md: never\n"
    "# commit company names).\n"
)


def write_tickers_file(
    path: Path, matches: dict[int, TickerMatch], unmatched: list[str]
) -> None:
    """Write the review file. Refuses to overwrite a file the user may have edited."""
    if path.exists():
        raise FileExistsError(
            f"{path} already exists and may hold manual edits. Edit it directly, "
            "or delete it to derive the list again."
        )
    lines = [_HEADER]
    for match in sorted(matches.values(), key=lambda m: m.ticker):
        lines.append(
            f"{match.ticker}\t{match.cik}\t{','.join(sorted(match.matched_by))}\t"
            f"{match.title}\t{','.join(sorted(match.case_ids))}\n"
        )
    for case_id in unmatched:
        lines.append(f"# unmatched: {case_id}\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")


def read_tickers_file(path: Path) -> dict[str, int]:
    """Ticker -> CIK from the reviewed file, ignoring comments and blank lines."""
    if not path.exists():
        raise FileNotFoundError(
            f"{path} doesn't exist. Run `recon.cli edgar fetch --from-dataset` to "
            "derive it, review it, then run `recon.cli edgar fetch`."
        )
    result: dict[str, int] = {}
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        try:
            result[parts[0].strip()] = int(parts[1])
        except (IndexError, ValueError) as exc:
            raise ValueError(
                f"{path} line {line_number}: expected TICKER<TAB>CIK, got {line!r}."
            ) from exc
    return result
