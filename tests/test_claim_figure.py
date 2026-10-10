"""Claims that state their figure as data (#148): `ClaimFigure`, and the
verifier checking it against the cited rows without reading the text.

Rows use real XBRL concept names with made-up values, so the data stays
synthetic. No model, no network.
"""

from typing import Any, get_args

import pytest
from pydantic import ValidationError

from recon.contracts import Claim, ClaimFigure
from recon.eval.claim_verifier import verifier_version
from recon.eval.verification import verify_claims
from recon.runtimes.answer import CLAIMS_SCHEMA, validate_answer
from recon.runtimes.evidence import RowIndex, resolve_claims
from recon.runtimes.langgraph import (
    AnswerResponse,
    ClaimResponse,
    FigureResponse,
)

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
    figure: dict[str, str], rows: list[dict[str, Any]], text: str | None = None
) -> Claim:
    return Claim(
        text=text or f"The figure is {figure['value'].lstrip('+-')}.",
        importance="key",
        evidence_refs=[row["ref"] for row in rows],
        figure=ClaimFigure(**figure),  # type: ignore[arg-type]
    )


def _verdict(
    figure: dict[str, str],
    cited: list[dict[str, Any]],
    *,
    evidence: list[dict[str, Any]] | None = None,
    text: str | None = None,
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
def test_the_texts_concept_isnt_read_when_a_figure_is_given() -> None:
    """The text names no concept the reader knows. Its periods aren't figures."""
    row = _row(REVENUE, 6_811e6, period="Q3")
    figure = {"kind": "level", "value": "6,811", "scale": "millions"}
    text = "Q3 FY2024 sales came in at $6,811 million."
    assert _verdict(figure, [row], text=text) == "SUPPORTED"


@pytest.mark.unit
def test_a_figure_that_looks_like_a_year_isnt_stripped_as_a_period() -> None:
    """A bare year is only a period when nothing marks it as a dollar figure
    instead, so a figure that happens to fall in 1900-2099 still reads."""
    row = _row(REVENUE, 2024e6)
    figure = {"kind": "level", "value": "2024", "scale": "millions"}
    text = "Revenue was $2024 million in FY2024."
    assert _verdict(figure, [row], text=text) == "SUPPORTED"


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "Revenue for the year ended December 31, 2024 was $450 million.",
        "Revenue for the year ended December 31 2024 was $450 million.",
        "Revenue for the year ended 31 December 2024 was $450 million.",
        "Revenue for the year ended 2024-12-31 was $450 million.",
        "Revenue for the year ended 12/31/2024 was $450 million.",
        "Revenue for the year ended June 30 was $450 million.",
        "For the fiscal year ended Dec. 31, 2024, revenue was $450 million.",
    ],
)
def test_a_calendar_dates_day_isnt_read_as_a_stated_number(text: str) -> None:
    """A full date names a period's end, so its day-of-month mustn't leak
    past the stripper and read as a second stated number."""
    row = _row(REVENUE, 450e6)
    figure = {"kind": "level", "value": "450", "scale": "millions"}
    assert _verdict(figure, [row], text=text) == "SUPPORTED"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("Revenue was $700 million in FY2024.", "450"),
        ("Revenue was $450 million in FY2024, up 80%.", "450"),
        ("Q3 came in at 450m, about 4% above the 430m expected.", "450"),
        ("Revenue was $6.8 billion in FY2024.", "6,811"),
    ],
)
def test_a_text_stating_another_number_than_its_figure_is_unverifiable(
    text: str, value: str
) -> None:
    """The figure is checked only when it is the one number the text states,
    so a claim can't pass on a figure its text doesn't say."""
    row = _row(REVENUE, 450e6 if value == "450" else 6_811e6)
    figure = {"kind": "level", "value": value, "scale": "millions"}
    [verification] = verify_claims([_claim(figure, [row], text)], [row]).claims
    assert verification.verdict == "UNVERIFIABLE"
    assert "text" in verification.reasoning


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "In 2024 revenue was 2024 million.",
        "In 2024 revenue was 2024m.",
        "Revenue was 2,024 million in 2024.",
        "Revenue grew 2000% in 2024.",
    ],
)
def test_a_figure_that_looks_like_a_year_is_still_read(text: str) -> None:
    """Only a year that names a period is skipped, not an amount."""
    if "%" in text:
        rows = [_row(REVENUE, 2_100e6), _row(REVENUE, 100e6, year=2023)]
        figure = {"kind": "growth", "value": "2000", "scale": "percent"}
    else:
        rows = [_row(REVENUE, 2_024e6)]
        value = "2,024" if "2,024" in text else "2024"
        figure = {"kind": "level", "value": value, "scale": "millions"}
    assert _verdict(figure, rows, text=text) == "SUPPORTED"


@pytest.mark.unit
def test_a_falling_growth_figure_matches_unsigned_text() -> None:
    rows = [_row(REVENUE, 360e6), _row(REVENUE, 400e6, year=2023)]
    figure = {"kind": "growth", "value": "-10.0", "scale": "percent"}
    text = "Revenue fell 10.0% from FY2023 to FY2024."
    assert _verdict(figure, rows, text=text) == "SUPPORTED"


@pytest.mark.unit
def test_growth_in_a_percentage_concept_is_unverifiable() -> None:
    """A change in a margin may be meant in points or relative terms. The text
    reader refuses it too."""
    rows = [
        _row("GrossMarginPct", 44.0, unit="PCT"),
        _row("GrossMarginPct", 40.0, year=2023, unit="PCT"),
    ]
    figure = {"kind": "growth", "value": "4.0", "scale": "percent"}
    [verification] = verify_claims([_claim(figure, rows)], rows).claims
    assert verification.verdict == "UNVERIFIABLE"
    assert "percentage" in verification.reasoning


@pytest.mark.unit
def test_growth_between_a_quarter_and_a_year_is_unverifiable() -> None:
    rows = [_row(REVENUE, 120e6, period="Q3"), _row(REVENUE, 400e6, year=2023)]
    figure = {"kind": "growth", "value": "-70.0", "scale": "percent"}
    [verification] = verify_claims([_claim(figure, rows)], rows).claims
    assert verification.verdict == "UNVERIFIABLE"
    assert "quarter" in verification.reasoning


@pytest.mark.unit
@pytest.mark.parametrize(
    ("numerator", "denominator"),
    [
        (_row("NetIncomeLoss", 36e6), _row(REVENUE, 450.0, unit="USD_M")),
        (
            _row("GrossMarginPct", 40.0, unit="PCT"),
            _row("OpMarginPct", 8.0, unit="PCT"),
        ),
    ],
)
def test_a_ratio_of_rows_in_other_units_or_percentages_is_unverifiable(
    numerator: dict[str, Any], denominator: dict[str, Any]
) -> None:
    figure = {"kind": "ratio", "value": "8.0", "scale": "percent"}
    rows = [numerator, denominator]
    [verification] = verify_claims([_claim(figure, rows)], rows).claims
    assert verification.verdict == "UNVERIFIABLE"
    assert "unit" in verification.reasoning


@pytest.mark.unit
def test_a_figure_citing_a_ref_with_no_row_is_unsupported() -> None:
    """A made-up ref is unsupported, as for a text claim that cites only it."""
    rows = [_row(REVENUE, 440e6, ref="E2024"), _row(REVENUE, 400e6, year=2023)]
    claim = Claim(
        text="Revenue grew 10%.",
        importance="key",
        evidence_refs=["E2024", "Ebogus"],
        figure=ClaimFigure(kind="growth", value="10", scale="percent"),
    )
    [verification] = verify_claims([claim], rows).claims
    assert verification.verdict == "UNSUPPORTED"
    assert "Ebogus" in verification.reasoning


@pytest.mark.unit
def test_a_figure_may_also_cite_filing_text() -> None:
    """A ref known to be a non-fact source isn't a missing row."""
    row = _row(REVENUE, 450e6, ref="E1")
    claim = Claim(
        text="Revenue was $450 million.",
        importance="key",
        evidence_refs=["E1", "K7"],
        figure=ClaimFigure(kind="level", value="450", scale="millions"),
    )
    [verification] = verify_claims([claim], [row], non_fact_refs={"K7"}).claims
    assert verification.verdict == "SUPPORTED"


@pytest.mark.unit
def test_a_text_claim_reads_its_rows_in_evidence_order() -> None:
    """Two versions filed the same day: the later one in the evidence is
    current, whatever order the claim cites them in."""
    first = _row("revenue", 400e6, ref="Ea")
    second = _row("revenue", 392e6, ref="Eb")
    claim = Claim(
        text="Revenue was $392 million in FY2024.",
        importance="key",
        evidence_refs=["Eb", "Ea"],
    )
    [verification] = verify_claims([claim], [first, second]).claims
    assert verification.verdict == "SUPPORTED"
    assert verification.evidence_refs == ["Eb"]


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
    for value in ("about 450", "12.5%", "$6,811", "\u221212.5", "1,23"):
        with pytest.raises(ValidationError):
            ClaimFigure(kind="level", value=value, scale="millions")


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
    assert verifier_version() == "3:tol=0"


@pytest.mark.unit
def test_a_figure_on_a_pure_ratio_row_is_a_percentage() -> None:
    row = _row("EffectiveIncomeTaxRateContinuingOperations", 0.109, unit="pure")
    figure = {"kind": "level", "value": "10.9", "scale": "percent"}
    assert _verdict(figure, [row]) == "SUPPORTED"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "verdict"), [("6.49", "UNVERIFIABLE"), ("7.5", "CONTRADICTED")]
)
def test_a_ratio_stated_as_a_multiple_isnt_contradicted(
    value: str, verdict: str
) -> None:
    """A turnover of 6.49 times is the quotient as a multiple, not 6.49%.
    It can't be checked as a percentage, but it isn't wrong either."""
    cogs = _row("CostOfGoodsAndServicesSold", 14_060e6)
    inventory = _row("InventoryNet", 2_168e6)
    figure = {"kind": "ratio", "value": value, "scale": "percent"}
    [verification] = verify_claims(
        [_claim(figure, [cogs, inventory])], [cogs, inventory]
    ).claims
    assert verification.verdict == verdict
    if verdict == "UNVERIFIABLE":
        assert "multiple" in verification.reasoning


@pytest.mark.unit
def test_the_answer_schemas_offer_a_figure_with_the_contracts_values() -> None:
    """The agent's claims may carry a figure (#148 PR 2). Its enums match
    `ClaimFigure`, so a value the model picks from them is never dropped."""
    item = CLAIMS_SCHEMA["items"]
    figure = item["properties"]["figure"]
    assert "figure" not in item["required"]
    for field in ("kind", "scale"):
        allowed = get_args(ClaimFigure.model_fields[field].annotation)
        assert figure["properties"][field]["enum"] == list(allowed)
        assert get_args(FigureResponse.model_fields[field].annotation) == allowed
    assert ClaimResponse.model_fields["figure"].default is None
    assert set(figure["required"]) == {"kind", "value", "scale"}


@pytest.mark.unit
def test_a_langgraph_claim_keeps_its_figure_through_resolution() -> None:
    response = AnswerResponse(
        answer="Revenue was $450 million.",
        claims=[
            ClaimResponse(
                text="Revenue was $450 million.",
                importance="key",
                evidence_refs=[],
                figure=FigureResponse(kind="level", value="450", scale="millions"),
            )
        ],
        confidence="high",
    )
    answer = validate_answer(response.model_dump(), RowIndex())
    assert answer.claims[0].figure == ClaimFigure(
        kind="level", value="450", scale="millions"
    )
