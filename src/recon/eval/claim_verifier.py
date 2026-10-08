"""Verify a numeric claim against the tool rows it rests on, with no model call
(#137, ADR 0034).

The verifier reads a claim such as "Revenue grew 12.5% from FY2023 to FY2024",
finds the rows for the concepts and periods it names, recomputes the figure
and compares. It handles three kinds of claim: a level ("revenue was $450
million"), a growth rate and a ratio ("net income was 8% of revenue"). A claim
it can't read, or one that also asserts a cause, is UNVERIFIABLE. It never
guesses.

Rows are the dicts `get_financial_fact` returns. When several rows describe one
period, the latest `filed` is current and an earlier one is a superseded value.
"""

from recon.contracts import ClaimFigure, ClaimVerification, Verdict
from recon.eval.claim_figure import reading_from_figure
from recon.eval.claim_reader import (
    Period,
    Reading,
    Row,
    Unreadable,
    needs_judgement,
    read_claim,
)

# Dollars per unit of a row's `unit`. A row in any other unit can't be compared.
_UNIT_DOLLARS = {"USD": 1.0, "USD_M": 1e6}
_EPSILON = 1e-9
# Bump when a rule changes what a claim's verdict is (ADR 0034, 0035).
# "2": claims can state their figure as data (#148).
# "3": a row in unit `pure` is read as a percentage (#145).
VERIFIER_RULES = "3"


def verifier_version(tolerance: float = 0.0) -> str:
    """The id a run records for how its claims were verified. Two runs'
    verdicts are comparable only when these match."""
    return f"{VERIFIER_RULES}:tol={tolerance:g}"


def verify_claim(
    claim_id: str,
    text: str,
    rows: list[Row],
    tolerance: float = 0.0,
    figure: ClaimFigure | None = None,
) -> ClaimVerification:
    """Check `text` against `rows` and return a verdict. With `figure`, its
    kind and number are checked against `rows` instead of reading `text`
    (#148). The text's cause and outlook rules still apply.

    A figure matches when it equals the recomputed value rounded to the
    precision the claim states ("8.3%" allows 0.05 points). `tolerance` adds
    to that allowance, in percentage points or in the row's unit.
    """
    verified = _verify(claim_id, text, rows, tolerance, figure)
    return verified.model_copy(update={"text": text})


def _verify(
    claim_id: str,
    text: str,
    rows: list[Row],
    tolerance: float,
    figure: ClaimFigure | None,
) -> ClaimVerification:
    """`verify_claim` before the claim text is attached to the result."""
    usable = [_as_percent(row) for row in rows if _is_usable(row)]
    try:
        if (why := needs_judgement(text)) is not None:
            raise Unreadable(why)
        if not usable:
            return _result(claim_id, "UNSUPPORTED", "The claim cites no usable rows.")
        reading = (
            read_claim(text, usable)
            if figure is None
            else reading_from_figure(figure, usable, text)
        )
        return _check(claim_id, reading, usable, tolerance)
    except Unreadable as unreadable:
        return _result(claim_id, "UNVERIFIABLE", str(unreadable))


def _as_percent(row: Row) -> Row:
    """EDGAR files rates as a fraction in unit `pure` (0.109), where claims
    state a percentage (10.9%). Such a row is read as `PCT`. A `pure` value
    beyond ±1 is a multiple or a count, not a fraction, and stays as it is."""
    if row.get("unit") != "pure" or abs(row["value"]) > 1:
        return row
    return {**row, "unit": "PCT", "value": row["value"] * 100}


def _is_usable(row: Row) -> bool:
    value = row.get("value")
    return (
        isinstance(row.get("concept"), str)
        and isinstance(row.get("fiscal_year"), int)
        and isinstance(row.get("fiscal_period"), str)
        and isinstance(value, int | float)
        and not isinstance(value, bool)
    )


def _result(
    claim_id: str,
    verdict: Verdict,
    reasoning: str,
    *,
    refs: list[str] | None = None,
    claimed: float | None = None,
    recomputed: float | None = None,
    allowed: float | None = None,
) -> ClaimVerification:
    return ClaimVerification(
        claim_id=claim_id,
        text="",
        verdict=verdict,
        evidence_refs=refs or [],
        claimed_value=claimed,
        recomputed_value=recomputed,
        tolerance=allowed,
        reasoning=reasoning,
    )


# ---- reading the claim ------------------------------------------------------


# ---- checking the claim -----------------------------------------------------


def _versions(rows: list[Row], concept: str, period: Period) -> list[Row]:
    """The distinct values filed for one concept and period, oldest first."""
    matching = [
        row
        for row in rows
        if row["concept"] == concept
        and (row["fiscal_year"], row["fiscal_period"]) == period
    ]
    if len({row["value"] for row in matching}) <= 1:
        return matching[-1:] if matching else []
    if not all(isinstance(row.get("filed"), str) for row in matching):
        raise Unreadable(
            f"{concept} for {period[1]} {period[0]} has several values and no filing dates."
        )
    versions: list[Row] = []
    for row in sorted(matching, key=lambda r: r["filed"]):
        if versions and versions[-1]["value"] == row["value"]:
            versions[-1] = row
        else:
            versions.append(row)
    return versions


def _periods_needed(reading: Reading) -> list[Period]:
    if reading.kind == "growth" and len(reading.periods) == 1:
        year, name = reading.periods[0]
        return [(year - 1, name), reading.periods[0]]
    return reading.periods


def _compute(reading: Reading, picked: dict[tuple[str, Period], Row]) -> float:
    """The figure the rows give, in the claim's own terms."""
    periods = _periods_needed(reading)
    if reading.kind == "level":
        return float(picked[(reading.concepts[0], periods[0])]["value"])
    if reading.kind == "growth":
        before = float(picked[(reading.concepts[0], periods[0])]["value"])
        after = float(picked[(reading.concepts[0], periods[1])]["value"])
        if before == 0:
            raise Unreadable("The earlier value is zero, so growth is undefined.")
        return (after - before) / before * 100
    top = float(picked[(reading.concepts[0], periods[0])]["value"])
    bottom = float(picked[(reading.concepts[1], periods[0])]["value"])
    if bottom == 0:
        raise Unreadable("The denominator is zero, so the ratio is undefined.")
    return top / bottom * 100


def _claimed_and_allowance(
    reading: Reading, unit: str | None, extra: float
) -> tuple[float, float]:
    """The claimed figure and how far a recomputed one may differ, both in the
    unit `_compute` returns."""
    half_step = 0.5 * 10**-reading.figure.decimals
    if reading.kind != "level" or unit == "PCT":
        return reading.figure.value * reading.sign, half_step + extra
    per_unit = _UNIT_DOLLARS.get(unit or "")
    if per_unit is None:
        raise Unreadable(f"The rows' unit {unit!r} isn't one the verifier converts.")
    scale = reading.scale_dollars / per_unit
    return reading.figure.value * scale, half_step * scale + extra


def _check(
    claim_id: str, reading: Reading, rows: list[Row], tolerance: float
) -> ClaimVerification:
    unit = next(r.get("unit") for r in rows if r["concept"] == reading.concepts[0])
    claimed, allowed = _claimed_and_allowance(reading, unit, tolerance)
    concepts = reading.concepts if reading.kind == "ratio" else reading.concepts[:1]
    wanted = [(c, p) for p in _periods_needed(reading) for c in concepts]
    versions = {key: _versions(rows, *key) for key in wanted}
    missing = [key for key, found in versions.items() if not found]
    if missing:
        return _missing(claim_id, reading, rows, missing[0], claimed, allowed)
    return _judge(claim_id, reading, rows, versions, claimed, allowed)


def _judge(
    claim_id: str,
    reading: Reading,
    rows: list[Row],
    versions: dict[tuple[str, Period], list[Row]],
    claimed: float,
    allowed: float,
) -> ClaimVerification:
    """Compare the claim with the current rows, then with the first-filed ones."""
    current = {key: found[-1] for key, found in versions.items()}
    earliest = {key: found[0] for key, found in versions.items()}
    now, now_refs = _compute(reading, current), _refs(current)
    then, then_refs = _compute(reading, earliest), _refs(earliest)
    numbers = {"claimed": claimed, "allowed": allowed}
    if abs(now - claimed) <= allowed + _EPSILON:
        reasoning = f"Recomputed {now:.4g}, claimed {claimed:.4g}."
        return _result(
            claim_id, "SUPPORTED", reasoning, refs=now_refs, recomputed=now, **numbers
        )
    if abs(then - claimed) <= allowed + _EPSILON:
        reasoning = (
            f"Matches the first filing ({then:.4g}). The current rows give {now:.4g}."
        )
        return _result(
            claim_id, "STALE", reasoning, refs=then_refs, recomputed=now, **numbers
        )
    reasoning = f"The rows give {now:.4g}, the claim says {claimed:.4g}."
    reasoning += _elsewhere(reading, rows, claimed, allowed)
    return _result(
        claim_id, "CONTRADICTED", reasoning, refs=now_refs, recomputed=now, **numbers
    )


def _refs(picked: dict[tuple[str, Period], Row]) -> list[str]:
    return [str(row["ref"]) for row in picked.values() if "ref" in row]


def _elsewhere(
    reading: Reading, rows: list[Row], claimed: float, allowed: float
) -> str:
    """For a level claim: the other period whose value the claim matches."""
    if reading.kind != "level":
        return ""
    for row in rows:
        if (
            row["concept"] == reading.concepts[0]
            and abs(float(row["value"]) - claimed) <= allowed + _EPSILON
        ):
            return (
                f" That is the value for {row['fiscal_period']} {row['fiscal_year']}."
            )
    return ""


def _missing(
    claim_id: str,
    reading: Reading,
    rows: list[Row],
    key: tuple[str, Period],
    claimed: float,
    allowed: float,
) -> ClaimVerification:
    """No row for a period the claim needs. A level that matches another
    period's value is a wrong period (CONTRADICTED); otherwise there is no
    evidence for the claim (UNSUPPORTED)."""
    concept, (year, name) = key
    note = _elsewhere(reading, rows, claimed, allowed)
    if note:
        return _result(
            claim_id,
            "CONTRADICTED",
            f"No {concept} row for {name} {year}." + note,
            claimed=claimed,
            allowed=allowed,
        )
    return _result(
        claim_id,
        "UNSUPPORTED",
        f"No {concept} row for {name} {year}.",
        claimed=claimed,
        allowed=allowed,
    )
