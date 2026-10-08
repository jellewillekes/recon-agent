"""Claims that state their figure as data (#148): `ClaimFigure`, and the
verifier checking it against the cited rows without reading the text.

Rows use real XBRL concept names with made-up values, so the data stays
synthetic. No model, no network.
"""

from typing import Any

import pytest
from pydantic import ValidationError

from recon.contracts import Claim, ClaimFigure
from recon.eval.claim_verifier import verifier_version
from recon.eval.verification import verify_claims
from recon.runtimes.evidence import RowIndex, resolve_claims

REVENUE = "RevenueFromContractWithCustomerExcludingAssessedTax"


def _row(
    concept: str,
    value: float,
    *,
    year: int = 2024,
    period: str = "FY",
    filed: str = "2025-02-14",
    ref: str | None = None,
    unit: str = "USD",
) -> dict[str, Any]:
    return {
        "ref": ref or f"E{concept[:4]}{year}{period}{filed[:4]}",
        "concept": concept,
        "fiscal_year": year,
        "fiscal_period": period,
        "value": value,
        "unit": unit,
        "filed": filed,
    }


def _claim(
    figure: dict[str, str], rows: list[dict[str, Any]], text: str = "See the figure."
) -> Claim:
    return Claim(
        text=text,
        importance="key",
        evidence_refs=[row["ref"] for row in rows],
        figure=ClaimFigure(**figure),  # type: ignore[arg-type]
    )


def _verdict(
    figure: dict[str, str],
    cited: list[dict[str, Any]],
    *,
    evidence: list[dict[str, Any]] | None = None,
    text: str = "See the figure.",
) -> str:
    claim = _claim(figure, cited, text)
    [verification] = verify_claims([claim], evidence or cited).claims
    return verification.verdict


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "scale", "verdict"),
    [
        ("6,811", "millions", "SUPPORTED"),
        ("6.811", "billions", "SUPPORTED"),
        ("6.8", "billions", "SUPPORTED"),
        ("6,811,000,000", "units", "SUPPORTED"),
        ("7,000", "millions", "CONTRADICTED"),
        ("6,812", "millions", "CONTRADICTED"),
    ],
)
def test_a_level_is_checked_against_its_one_row(
    value: str, scale: str, verdict: str
) -> None:
    """The stated decimals set the rounding allowance, as for text claims."""
    row = _row(REVENUE, 6_811e6, period="Q3")
    assert _verdict({"kind": "level", "value": value, "scale": scale}, [row]) == verdict


@pytest.mark.unit
def test_the_text_isnt_read_when_a_figure_is_given() -> None:
    """The text names no concept the reader knows and states other numbers."""
    row = _row(REVENUE, 6_811e6, period="Q3")
    figure = {"kind": "level", "value": "6,811", "scale": "millions"}
    text = "Q3 came in at 6.8bn, about 4% above the 6.5bn guided."
    assert _verdict(figure, [row], text=text) == "SUPPORTED"


@pytest.mark.unit
def test_a_claim_that_gives_a_cause_isnt_passed_on_its_number() -> None:
    row = _row(REVENUE, 6_811e6, period="Q3")
    figure = {"kind": "level", "value": "6,811", "scale": "millions"}
    text = "Revenue was $6,811 million, driven by data center demand."
    assert _verdict(figure, [row], text=text) == "UNVERIFIABLE"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "verdict"),
    [("10.0", "SUPPORTED"), ("10", "SUPPORTED"), ("-10.0", "CONTRADICTED")],
)
def test_growth_is_checked_from_two_periods_of_one_concept(
    value: str, verdict: str
) -> None:
    rows = [_row(REVENUE, 440e6), _row(REVENUE, 400e6, year=2023)]
    figure = {"kind": "growth", "value": value, "scale": "percent"}
    assert _verdict(figure, rows) == verdict


@pytest.mark.unit
def test_a_ratio_divides_the_first_cited_row_by_the_second() -> None:
    income, revenue = _row("NetIncomeLoss", 36e6), _row(REVENUE, 450e6)
    figure = {"kind": "ratio", "value": "8.0", "scale": "percent"}
    assert _verdict(figure, [income, revenue]) == "SUPPORTED"
    assert _verdict(figure, [revenue, income]) == "CONTRADICTED"


@pytest.mark.unit
def test_a_figure_matching_the_first_filing_is_stale() -> None:
    first = _row(REVENUE, 400e6, year=2023, filed="2024-02-14", ref="Efirst")
    restated = _row(REVENUE, 392e6, year=2023, filed="2025-02-14", ref="Erestated")
    figure = {"kind": "level", "value": "400", "scale": "millions"}
    assert _verdict(figure, [first, restated]) == "STALE"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("figure", "rows", "reason"),
    [
        (
            {"kind": "level", "value": "450", "scale": "millions"},
            [_row(REVENUE, 450e6), _row("NetIncomeLoss", 36e6)],
            "one row",
        ),
        (
            {"kind": "growth", "value": "10", "scale": "percent"},
            [_row(REVENUE, 440e6)],
            "two periods",
        ),
        (
            {"kind": "ratio", "value": "8", "scale": "percent"},
            [_row("NetIncomeLoss", 36e6), _row(REVENUE, 450e6, year=2023)],
            "one period",
        ),
        (
            {"kind": "level", "value": "450", "scale": "percent"},
            [_row(REVENUE, 450e6)],
            "scale",
        ),
        (
            {"kind": "growth", "value": "10", "scale": "millions"},
            [_row(REVENUE, 440e6), _row(REVENUE, 400e6, year=2023)],
            "scale",
        ),
        (
            {"kind": "level", "value": "about 450", "scale": "millions"},
            [_row(REVENUE, 450e6)],
            "number",
        ),
    ],
)
def test_a_figure_its_rows_dont_fit_is_unverifiable(
    figure: dict[str, str], rows: list[dict[str, Any]], reason: str
) -> None:
    claim = _claim(figure, rows)
    [verification] = verify_claims([claim], rows).claims
    assert verification.verdict == "UNVERIFIABLE"
    assert reason in verification.reasoning


@pytest.mark.unit
def test_a_figure_is_checked_only_against_the_rows_it_cites() -> None:
    """Another row with the same concept and period isn't borrowed."""
    cited = _row(REVENUE, 450e6, ref="Ecited")
    other = _row("NetIncomeLoss", 36e6, ref="Eother")
    figure = {"kind": "level", "value": "450", "scale": "millions"}
    assert _verdict(figure, [cited], evidence=[cited, other]) == "SUPPORTED"


@pytest.mark.unit
def test_a_figure_that_cites_nothing_is_unsupported() -> None:
    claim = Claim(
        text="Revenue was $450 million.",
        importance="key",
        evidence_refs=[],
        figure=ClaimFigure(kind="level", value="450", scale="millions"),
    )
    [verification] = verify_claims([claim], [_row(REVENUE, 450e6)]).claims
    assert verification.verdict == "UNSUPPORTED"


@pytest.mark.unit
def test_a_claim_without_a_figure_is_read_from_its_text_as_before() -> None:
    row = _row("revenue", 450e6, unit="USD")
    claim = Claim(
        text="Revenue was $450 million in FY2024.",
        importance="key",
        evidence_refs=[row["ref"]],
    )
    assert claim.figure is None
    [verification] = verify_claims([claim], [row]).claims
    assert verification.verdict == "SUPPORTED"


@pytest.mark.unit
def test_figure_fields_are_validated() -> None:
    with pytest.raises(ValidationError):
        ClaimFigure(kind="average", value="1", scale="millions")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ClaimFigure(kind="level", value="1", scale="dozens")  # type: ignore[arg-type]


@pytest.mark.unit
def test_resolve_claims_keeps_a_valid_figure_and_drops_a_malformed_one() -> None:
    """A malformed figure from the model mustn't lose the answer. The claim
    is kept and read from its text."""
    raw: list[dict[str, Any]] = [
        {
            "text": "a",
            "importance": "key",
            "evidence_refs": [],
            "figure": {"kind": "level", "value": "450", "scale": "millions"},
        },
        {
            "text": "b",
            "importance": "key",
            "evidence_refs": [],
            "figure": {"kind": "level", "value": "450", "scale": "dozens"},
        },
        {"text": "c", "importance": "key", "evidence_refs": []},
    ]

    claims, _, _ = resolve_claims(raw, RowIndex())

    assert claims[0].figure == ClaimFigure(kind="level", value="450", scale="millions")
    assert claims[1].figure is None
    assert claims[2].figure is None


@pytest.mark.unit
def test_the_verifier_version_moves_with_figures() -> None:
    assert verifier_version() == "2:tol=0"
