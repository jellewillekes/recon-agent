"""Tests for the numeric claim verifier (src/recon/eval/claim_verifier.py).

No model, no network. The verifier is run over the labelled set in
evals/verification-claims.yaml, plus focused cases for the rules the ADR
(0034) states.
"""

from collections import Counter
from typing import Any

import pytest

from recon.eval.claim_set import VERDICTS, LabelledClaim, load_claim_set
from recon.eval.claim_verifier import verify_claim

CLAIMS = load_claim_set()
NUMERIC = [claim for claim in CLAIMS if claim.scope == "numeric"]
JUDGEMENT = [claim for claim in CLAIMS if claim.scope == "llm"]


def _row(
    value: float,
    *,
    year: int = 2024,
    period: str = "FY",
    concept: str = "revenue",
    unit: str = "USD_M",
    filed: str | None = "2025-02-14",
    ref: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ref": ref or f"E{concept[:3]}{year}{period}{int(value)}",
        "concept": concept,
        "fiscal_year": year,
        "fiscal_period": period,
        "value": value,
        "unit": unit,
    }
    if filed is not None:
        row["filed"] = filed
    return row


@pytest.mark.parametrize("claim", NUMERIC, ids=lambda claim: claim.id)
def test_numeric_claims_get_their_label(claim: LabelledClaim) -> None:
    result = verify_claim(claim.id, claim.text, claim.rows)
    assert result.verdict == claim.expected_verdict, result.reasoning


@pytest.mark.parametrize("claim", JUDGEMENT, ids=lambda claim: claim.id)
def test_claims_needing_judgement_are_not_guessed(claim: LabelledClaim) -> None:
    """A cause or outlook can't be recomputed, so the verifier says so."""
    result = verify_claim(claim.id, claim.text, claim.rows)
    assert result.verdict == "UNVERIFIABLE", result.reasoning


def test_precision_and_recall_per_verdict(capsys: pytest.CaptureFixture[str]) -> None:
    """Every numeric verdict is predicted and labelled alike. Printed with -s."""
    predicted = {c.id: verify_claim(c.id, c.text, c.rows).verdict for c in NUMERIC}
    labelled = {c.id: c.expected_verdict for c in NUMERIC}
    hits: Counter[str] = Counter()
    for claim_id, verdict in predicted.items():
        if verdict == labelled[claim_id]:
            hits[verdict] += 1
    with capsys.disabled():
        print()
        for verdict in VERDICTS:
            n_predicted = sum(1 for v in predicted.values() if v == verdict)
            n_labelled = sum(1 for v in labelled.values() if v == verdict)
            precision = hits[verdict] / n_predicted if n_predicted else float("nan")
            recall = hits[verdict] / n_labelled if n_labelled else float("nan")
            print(f"{verdict:<20} precision={precision:.2f} recall={recall:.2f}")
    assert predicted == labelled


def test_evidence_refs_are_the_rows_the_value_came_from() -> None:
    rows = [
        _row(400.0, year=2023, ref="Eold"),
        _row(450.0, year=2024, ref="Enew"),
        _row(99.0, year=2022, ref="Eunused"),
    ]
    result = verify_claim("c", "Revenue grew 12.5% from FY2023 to FY2024.", rows)
    assert result.verdict == "SUPPORTED"
    assert sorted(result.evidence_refs) == ["Enew", "Eold"]


def test_values_are_reported() -> None:
    rows = [_row(400.0, year=2023), _row(450.0)]
    result = verify_claim("c", "Revenue grew 12.0% from FY2023 to FY2024.", rows)
    assert result.verdict == "CONTRADICTED"
    assert result.claimed_value == pytest.approx(12.0)
    assert result.recomputed_value == pytest.approx(12.5)


def test_claimed_precision_sets_the_rounding_rule() -> None:
    rows = [_row(400.0, year=2023), _row(449.0)]
    one_decimal = verify_claim("c", "Revenue grew 12.4% in FY2024.", rows)
    whole_number = verify_claim("c", "Revenue grew 12% in FY2024.", rows)
    assert one_decimal.verdict == "CONTRADICTED"
    assert whole_number.verdict == "SUPPORTED"


def test_extra_tolerance_widens_the_match() -> None:
    rows = [_row(450.0)]
    text = "Revenue was $451.0 million in FY2024."
    assert verify_claim("c", text, rows).verdict == "CONTRADICTED"
    widened = verify_claim("c", text, rows, tolerance=1.0)
    assert widened.verdict == "SUPPORTED"
    assert widened.tolerance is not None


def test_latest_filing_decides_the_current_value() -> None:
    rows = [
        _row(100.0, filed="2024-01-01", ref="Efirst"),
        _row(90.0, filed="2025-01-01", ref="Elast"),
    ]
    current = verify_claim("c", "Revenue was $90.0 million in FY2024.", rows)
    superseded = verify_claim("c", "Revenue was $100.0 million in FY2024.", rows)
    assert current.verdict == "SUPPORTED"
    assert current.evidence_refs == ["Elast"]
    assert superseded.verdict == "STALE"


def test_unknown_unit_is_not_converted() -> None:
    thousands = [_row(450_000.0, unit="USD_K")]
    result = verify_claim("c", "Revenue was $450.0 million in FY2024.", thousands)
    assert result.verdict == "UNVERIFIABLE"  # unit the verifier doesn't know


def test_causal_wording_is_not_verified_on_its_number_alone() -> None:
    rows = [_row(450.0)]
    text = "Revenue was $450.0 million in FY2024, driven by Services."
    assert verify_claim("c", text, rows).verdict == "UNVERIFIABLE"


@pytest.mark.parametrize(
    "text",
    ["", "   ", "%%%", "Revenue $ million FY", "FY2024 FY2023 FY2022 revenue 5%"],
)
def test_garbage_text_never_raises(text: str) -> None:
    result = verify_claim("c", text, [_row(450.0)])
    assert result.verdict in VERDICTS


def test_rows_missing_fields_never_raise() -> None:
    rows: list[dict[str, Any]] = [{"concept": "revenue"}, {}]
    result = verify_claim("c", "Revenue was $450.0 million in FY2024.", rows)
    assert result.verdict in VERDICTS


def test_result_carries_the_claim_id_and_a_reason() -> None:
    result = verify_claim("claim-7", "Revenue was strong.", [_row(450.0)])
    assert result.claim_id == "claim-7"
    assert result.reasoning


@pytest.mark.parametrize(
    "text",
    [
        "Revenue was not $480 million in FY2024.",
        "Revenue didn't fall in FY2024.",
        "Revenue never reached $500 million by FY2024.",
    ],
)
def test_negated_claims_are_not_read_as_their_positive(text: str) -> None:
    """ "Not $480 million" is true when the value is 450, so it must not come
    back CONTRADICTED."""
    result = verify_claim("c", text, [_row(450.0)])
    assert result.verdict == "UNVERIFIABLE"


@pytest.mark.parametrize(
    ("text", "verdict"),
    [
        ("Margin was 10.9% in FY2024.", "SUPPORTED"),
        ("Margin was 12.0% in FY2024.", "CONTRADICTED"),
    ],
)
def test_a_pure_ratio_row_is_read_as_a_percentage(text: str, verdict: str) -> None:
    """EDGAR files rates such as an effective tax rate as a fraction in unit
    `pure`: 0.109 is 10.9%."""
    rows = [_row(0.109, concept="margin", unit="pure")]
    assert verify_claim("c", text, rows).verdict == verdict


def test_a_pure_row_that_isnt_a_fraction_is_not_converted() -> None:
    """`pure` also holds multiples such as a leverage ratio of 3.5."""
    rows = [_row(3.5, concept="leverage", unit="pure")]
    result = verify_claim("c", "Leverage was 350% in FY2024.", rows)
    assert result.verdict == "UNVERIFIABLE"


def test_a_text_ratio_of_rows_in_different_units_is_unverifiable() -> None:
    rows = [
        _row(0.109, concept="margin", unit="pure"),
        _row(450.0, concept="revenue", unit="USD_M"),
    ]
    result = verify_claim("c", "Margin was 2.4% of revenue in FY2024.", rows)
    assert result.verdict == "UNVERIFIABLE"


def test_a_growth_claim_over_a_concept_with_one_non_fraction_row_stays_consistent() -> (
    None
):
    """A tax rate of 1.5 in FY2023 isn't a fraction, so it stays `pure`. The
    0.109 row for FY2024 must then also stay `pure` rather than convert to
    `PCT`, or the growth would divide mismatched scales and read a true claim
    as CONTRADICTED."""
    rows = [
        _row(1.5, year=2023, concept="tax_rate", unit="pure"),
        _row(0.109, year=2024, concept="tax_rate", unit="pure"),
    ]
    result = verify_claim("c", "Tax rate fell from FY2023 to FY2024 by 92.7%.", rows)
    assert result.verdict == "SUPPORTED"


@pytest.mark.parametrize(
    "text",
    [
        "Margin fell 27.3% from FY2023 to FY2024.",
        "Margin fell 4.1% from FY2023 to FY2024.",
    ],
)
def test_a_change_in_a_rate_filed_as_pure_is_not_read(text: str) -> None:
    """A rate from 15% to 10.9% fell 4.1 points or 27.3% relative. The claim
    doesn't say which, so it is UNVERIFIABLE, as for any percentage concept
    (ADR 0034), rather than CONTRADICTED under one reading."""
    rows = [
        _row(0.15, year=2023, concept="margin", unit="pure"),
        _row(0.109, concept="margin", unit="pure"),
    ]
    assert verify_claim("c", text, rows).verdict == "UNVERIFIABLE"
