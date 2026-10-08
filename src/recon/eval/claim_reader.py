"""Read a numeric claim: which concepts and periods it names, and the one
figure it states (#137, ADR 0034).

`read_claim` returns a `Reading` or raises `Unreadable` with the reason. It
reads a level ("revenue was $450 million"), a growth rate ("grew 12.5% from
FY2023 to FY2024") and a ratio ("net income was 8% of revenue"). Anything
else, including a claim that also asserts a cause, is unreadable. The reader
never guesses.
"""

import re
from dataclasses import dataclass
from typing import Any, Literal

Period = tuple[int, str]
Row = dict[str, Any]

_SCALE_DOLLARS = {
    "trillion": 1e12,
    "billion": 1e9,
    "bn": 1e9,
    "b": 1e9,
    "million": 1e6,
    "mm": 1e6,
    "m": 1e6,
    "thousand": 1e3,
    "k": 1e3,
}

_CAUSAL = re.compile(
    r"\b(?:driven|due to|because|primarily|mainly|mostly|largely|thanks to|"
    r"as a result|attribut\w*|caused?|owing to|results? from|came from|"
    r"expects?|guidance|outlook|forecast\w*)\b",
    re.IGNORECASE,
)
_NEGATION = re.compile(r"\b(?:not|never|no|neither|nor|without)\b|n't\b", re.IGNORECASE)
_UP = re.compile(
    r"\b(?:grew|grow\w*|increas\w*|rose|rise\w*|up|higher|gain\w*|expand\w*)\b",
    re.IGNORECASE,
)
_DOWN = re.compile(
    r"\b(?:fell|fall\w*|declin\w*|decreas\w*|drop\w*|down|lower|shr[ua]nk|"
    r"contract\w*|reduc\w*)\b",
    re.IGNORECASE,
)
_AMOUNT = re.compile(
    r"(\$)?\s*(\d[\d,]*(?:\.(\d+))?)\s*"
    r"(trillion|billion|million|thousand|bn|mm|[kmb])?\b",
    re.IGNORECASE,
)
_PERCENT = re.compile(r"(\d+(?:\.(\d+))?)\s*(?:%|percent\b)", re.IGNORECASE)
_PERCENT_OF = re.compile(r"(?:%|percent)\s+of\b", re.IGNORECASE)
_PERIOD = re.compile(
    r"\bQ([1-4])\s*(?:of\s+)?(?:FY\s*|fiscal\s+(?:year\s+)?)?(\d{4})\b"
    r"|\b(?:FY|fiscal(?:\s+year)?)\s*(\d{4})\b"
    r"|\b((?:19|20)\d{2})\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Figure:
    """A number as the claim wrote it, and how many decimals it stated."""

    value: float
    decimals: int


@dataclass(frozen=True)
class Reading:
    """What a claim asserts: its kind, its concepts and periods, its figure."""

    kind: Literal["level", "growth", "ratio"]
    concepts: list[str]
    periods: list[Period]
    figure: Figure
    sign: int
    scale_dollars: float


class Unreadable(Exception):
    """The claim can't be checked by recomputation. The message says why."""


def read_claim(text: str, rows: list[Row]) -> Reading:
    """Turn the claim text into a `Reading`, or raise `Unreadable`."""
    units = {row["concept"]: row.get("unit") for row in rows}
    concepts = _find_concepts(text, list(units))
    if not concepts:
        raise Unreadable("No concept in the claim matches the evidence rows.")
    amounts = _amounts(text)
    percents = [
        (float(m.group(1)), len(m.group(2) or "")) for m in _PERCENT.finditer(text)
    ]
    periods = _find_periods(_AMOUNT.sub(_blank_amount, text))
    up, down = _UP.search(text), _DOWN.search(text)
    if up and down:
        raise Unreadable("The claim mixes rising and falling wording.")
    sign = 1 if up else -1 if down else 0
    if len(concepts) == 2 and _PERCENT_OF.search(text) and len(percents) == 1:
        figure = Figure(*percents[0])
        return Reading("ratio", concepts, _one(periods), figure, 1, 1.0)
    if len(concepts) != 1:
        raise Unreadable("The claim names more than one concept.")
    if units[concepts[0]] == "PCT":
        if sign or len(percents) != 1:
            raise Unreadable("A change in a percentage concept isn't read.")
        return Reading("level", concepts, _one(periods), Figure(*percents[0]), 1, 1.0)
    if sign and len(percents) == 1 and not amounts:
        if len(periods) > 2 or not periods:
            raise Unreadable("A growth claim needs one or two periods.")
        return Reading(
            "growth",
            concepts,
            sorted(periods, key=_order),
            Figure(*percents[0]),
            sign,
            1.0,
        )
    if len(amounts) == 1 and not percents and not sign:
        figure, scale = amounts[0]
        return Reading("level", concepts, _one(periods), figure, 1, scale)
    raise Unreadable(
        "The claim doesn't state exactly one figure in a form that is read."
    )


def _one(periods: list[Period]) -> list[Period]:
    if len(periods) != 1:
        raise Unreadable("The claim needs exactly one period.")
    return periods


def _order(period: Period) -> tuple[int, int]:
    year, name = period
    return (year, 5 if name == "FY" else int(name[1]))


def _amounts(text: str) -> list[tuple[Figure, float]]:
    """Dollar amounts: a number with a `$` or a scale word, and its scale."""
    found = []
    for match in _AMOUNT.finditer(text):
        dollar, number, decimals, scale = match.groups()
        if not dollar and not scale:
            continue
        factor = _SCALE_DOLLARS[scale.lower()] if scale else 1.0
        found.append(
            (Figure(float(number.replace(",", "")), len(decimals or "")), factor)
        )
    return found


def _blank_amount(match: re.Match[str]) -> str:
    """Blank out dollar amounts so "$2000 million" isn't read as a year."""
    dollar, _, _, scale = match.groups()
    return " " if dollar or scale else match.group(0)


def _find_periods(text: str) -> list[Period]:
    periods: list[Period] = []
    for match in _PERIOD.finditer(text):
        quarter, quarter_year, fiscal_year, bare_year = match.groups()
        if quarter:
            periods.append((int(quarter_year), f"Q{quarter}"))
        else:
            periods.append((int(fiscal_year or bare_year), "FY"))
    return list(dict.fromkeys(periods))


def _names(concept: str) -> list[str]:
    """How a claim might spell a concept: `net_income` as "net income", and
    `gross_margin_pct` also as "gross margin". CamelCase concepts are split."""
    words = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", concept).replace("_", " ").lower()
    names = [words]
    for suffix in (" pct", " percent"):
        if words.endswith(suffix):
            names.append(words.removesuffix(suffix))
    return names


def _find_concepts(text: str, concepts: list[str]) -> list[str]:
    """Concepts the text names, in order of appearance. A name inside a longer
    one ("income" in "net income") doesn't count."""
    spans: list[tuple[int, int, str]] = []
    for concept in concepts:
        for name in _names(concept):
            match = re.search(rf"\b{re.escape(name)}s?\b", text, re.IGNORECASE)
            if match:
                spans.append((match.start(), match.end(), concept))
                break
    kept = [
        span
        for span in spans
        if not any(
            other[0] <= span[0]
            and span[1] <= other[1]
            and other[1] - other[0] > span[1] - span[0]
            for other in spans
        )
    ]
    return [concept for _, _, concept in sorted(kept)]


def needs_judgement(text: str) -> str | None:
    """Why recomputing the claim's number can't settle it, or None. A claim
    that says why, looks ahead or denies a figure is about more than the
    figure: "not $480 million" is true when the value is 450."""
    if _CAUSAL.search(text):
        return "The claim also asserts a cause or an outlook."
    if _NEGATION.search(text):
        return "The claim is negated, so its figure alone doesn't settle it."
    return None
