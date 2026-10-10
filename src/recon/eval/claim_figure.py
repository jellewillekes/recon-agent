"""Turn a claim's structured figure and its cited rows into a `Reading`
(#148, ADR 0040).

The model says only what kind of figure it states and the number. The
concept and the periods come from the rows the claim cites, so nothing is
read from the claim's text. A figure its rows don't fit raises `Unreadable`
with the reason. So does a text that doesn't state the figure, or states
another number: a claim mustn't pass on a figure its text doesn't say.
"""

import re

from recon.contracts import ClaimFigure
from recon.eval.claim_reader import (
    Figure,
    Period,
    Reading,
    Row,
    Unreadable,
    period_order,
)

_NUMBER = re.compile(r"^([+-]?)(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?$")
_SCALE_DOLLARS = {"units": 1.0, "thousands": 1e3, "millions": 1e6, "billions": 1e9}
_TEXT_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
# Periods name rows, not figures, so their digits aren't compared. A bare
# year counts as a period only when no "$", scale word or % marks it as an
# amount: "$2024 million" is a figure. A calendar date ("December 31, 2024",
# "31 December 2024", "June 30", "2024-12-31", "12/31/2024") is stripped
# whole, so its day and month numbers don't leak out on their own.
_MONTH = (
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?"
    r"|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
)
_TEXT_PERIOD = re.compile(
    rf"\b(?:{_MONTH})\b\.?\s+\d{{1,2}}\b(?:,?\s*(?:19|20)\d{{2}}\b)?"
    rf"|\b\d{{1,2}}\s+(?:{_MONTH})\b\.?(?:,?\s*(?:19|20)\d{{2}}\b)?"
    r"|\b(?:19|20)\d{2}-\d{1,2}-\d{1,2}\b|\b\d{1,2}/\d{1,2}/(?:19|20)?\d{2}\b"
    r"|\b(?:Q[1-4]|H[12])\b|\b(?:FY|fiscal(?:\s+year)?)\s*\d{4}\b"
    r"|(?<![$\d.,])\b(?:19|20)\d{2}\b"
    r"(?!\s*(?:%|(?:percent|trillion|billion|million|thousand|units?|bn|mm|[kmb])\b))(?![.,]\d)",
    re.IGNORECASE,
)


def reading_from_figure(figure: ClaimFigure, rows: list[Row], text: str) -> Reading:
    """The `Reading` of `figure` over `rows`, the claim's cited rows in the
    order it cites them. `text` must state the figure and no other number."""
    number = _parse(figure.value)
    _check_text(figure.value, number, text)
    keys = list(dict.fromkeys(_key(row) for row in rows))
    concepts = list(dict.fromkeys(concept for concept, _ in keys))
    periods = list(dict.fromkeys(period for _, period in keys))
    if figure.kind == "level":
        if len(keys) != 1:
            raise Unreadable("A level must cite one row, or versions of one row.")
        return _level(figure, number, concepts, periods, rows)
    if figure.scale != "percent":
        raise Unreadable(f"A {figure.kind} figure needs the percent scale.")
    units = {row.get("unit") for row in rows}
    if figure.kind == "growth":
        if len(concepts) != 1 or len(periods) != 2:
            raise Unreadable("A growth figure must cite one concept in two periods.")
        if "PCT" in units:
            raise Unreadable(
                "A change in a percentage concept isn't read: it may be meant in "
                "points or in relative terms."
            )
        if [period == "FY" for _, period in periods].count(True) == 1:
            raise Unreadable("Growth from a quarter to a full year isn't checked.")
        sign = -1 if number.value < 0 else 1
        ordered = sorted(periods, key=period_order)
        return Reading("growth", concepts, ordered, _abs(number), sign, 1.0)
    if len(concepts) != 2 or len(periods) != 1:
        raise Unreadable("A ratio must cite two concepts in one period.")
    if len(units) != 1 or "PCT" in units:
        raise Unreadable(
            f"A ratio needs both rows in the same unit; they are in {sorted(map(str, units))}."
        )
    return Reading("ratio", concepts, periods, number, 1, 1.0)


def _level(
    figure: ClaimFigure,
    number: Figure,
    concepts: list[str],
    periods: list[Period],
    rows: list[Row],
) -> Reading:
    percent_row = rows[0].get("unit") == "PCT"
    if (figure.scale == "percent") != percent_row:
        raise Unreadable(
            f"The figure's scale {figure.scale!r} doesn't fit the row's unit "
            f"{rows[0].get('unit')!r}."
        )
    scale = 1.0 if percent_row else _SCALE_DOLLARS[figure.scale]
    return Reading("level", concepts, periods, number, 1, scale)


def _parse(value: str) -> Figure:
    match = _NUMBER.match(value.strip())
    if match is None:
        raise Unreadable(f"The figure's value {value!r} isn't a plain number.")
    sign, whole, fraction = match.groups()
    number = float(whole.replace(",", "") + "." + (fraction or "0"))
    return Figure(-number if sign == "-" else number, len(fraction or ""))


def _check_text(value: str, number: Figure, text: str) -> None:
    stated = [
        float(match.group().rstrip(",").replace(",", ""))
        for match in _TEXT_NUMBER.finditer(_TEXT_PERIOD.sub(" ", text))
    ]
    if abs(number.value) not in stated:
        raise Unreadable(f"The claim's text doesn't state its figure {value!r}.")
    if any(n != abs(number.value) for n in stated):
        raise Unreadable(
            f"The claim's text states numbers other than its figure {value!r}."
        )


def _abs(number: Figure) -> Figure:
    return Figure(abs(number.value), number.decimals)


def _key(row: Row) -> tuple[str, Period]:
    return row["concept"], (row["fiscal_year"], row["fiscal_period"])
