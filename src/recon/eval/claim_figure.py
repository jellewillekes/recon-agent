"""Turn a claim's structured figure and its cited rows into a `Reading`
(#148, ADR 0040).

The model says only what kind of figure it states and the number. The
concept and the periods come from the rows the claim cites, so nothing is
read from the claim's text. A figure its rows don't fit raises `Unreadable`
with the reason.
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


def reading_from_figure(figure: ClaimFigure, rows: list[Row]) -> Reading:
    """The `Reading` of `figure` over `rows`, the claim's cited rows in the
    order it cites them."""
    number = _parse(figure.value)
    keys = list(dict.fromkeys(_key(row) for row in rows))
    concepts = list(dict.fromkeys(concept for concept, _ in keys))
    periods = list(dict.fromkeys(period for _, period in keys))
    if figure.kind == "level":
        if len(keys) != 1:
            raise Unreadable("A level must cite one row, or versions of one row.")
        return _level(figure, number, concepts, periods, rows)
    if figure.scale != "percent":
        raise Unreadable(f"A {figure.kind} figure needs the percent scale.")
    if figure.kind == "growth":
        if len(concepts) != 1 or len(periods) != 2:
            raise Unreadable("A growth figure must cite one concept in two periods.")
        sign = -1 if number.value < 0 else 1
        ordered = sorted(periods, key=period_order)
        return Reading("growth", concepts, ordered, _abs(number), sign, 1.0)
    if len(concepts) != 2 or len(periods) != 1:
        raise Unreadable("A ratio must cite two concepts in one period.")
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


def _abs(number: Figure) -> Figure:
    return Figure(abs(number.value), number.decimals)


def _key(row: Row) -> tuple[str, Period]:
    return row["concept"], (row["fiscal_year"], row["fiscal_period"])
