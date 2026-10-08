"""Tests for `eval/verification.py`: claims in, a `VerificationReport` out.

No model, no network. Rows are shaped like `get_financial_fact` output.
"""

from typing import Any

import pytest

from recon.contracts import Claim, ClaimVerification, Verdict
from recon.eval.verification import build_report, verify_claims


def _row(
    value: float, year: int = 2024, ref: str = "E1", concept: str = "revenue"
) -> dict[str, Any]:
    return {
        "ref": ref,
        "concept": concept,
        "fiscal_year": year,
        "fiscal_period": "FY",
        "value": value,
        "unit": "USD_M",
        "filed": "2025-02-14",
    }


def _claim(text: str, refs: list[str] | None = None) -> Claim:
    return Claim(text=text, importance="key", evidence_refs=refs or [])


def _verification(verdict: Verdict, refs: list[str] | None = None) -> ClaimVerification:
    return ClaimVerification(
        claim_id="c",
        text="t",
        verdict=verdict,
        evidence_refs=["E1"] if refs is None else refs,
        claimed_value=None,
        recomputed_value=None,
        tolerance=None,
        reasoning="r",
    )


@pytest.mark.unit
def test_no_claims_is_unverifiable_with_no_scores() -> None:
    report = build_report([])
    assert report.overall_verdict == "UNVERIFIABLE"
    assert report.claims == []
    assert report.grounding_score is None
    assert report.correctness_score is None
    assert report.freshness_score is None
    assert set(report.counts.values()) == {0}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("verdicts", "overall"),
    [
        (["SUPPORTED", "SUPPORTED"], "SUPPORTED"),
        (["SUPPORTED", "UNVERIFIABLE"], "PARTIALLY_SUPPORTED"),
        (["UNVERIFIABLE", "UNVERIFIABLE"], "UNVERIFIABLE"),
        (["SUPPORTED", "UNSUPPORTED"], "UNSUPPORTED"),
        (["SUPPORTED", "UNSUPPORTED", "STALE"], "STALE"),
        (["SUPPORTED", "STALE", "CONTRADICTED"], "CONTRADICTED"),
        (["CONTRADICTED"], "CONTRADICTED"),
    ],
)
def test_overall_verdict_is_the_worst_claim(
    verdicts: list[Verdict], overall: Verdict
) -> None:
    report = build_report([_verification(v) for v in verdicts])
    assert report.overall_verdict == overall


@pytest.mark.unit
def test_counts_and_scores() -> None:
    report = build_report(
        [
            _verification("SUPPORTED"),
            _verification("SUPPORTED"),
            _verification("STALE"),
            _verification("CONTRADICTED"),
            _verification("UNVERIFIABLE", refs=[]),
        ]
    )
    assert report.counts["SUPPORTED"] == 2
    assert report.counts["UNVERIFIABLE"] == 1
    assert sum(report.counts.values()) == 5
    # Checked claims are the four the verifier could read.
    assert report.correctness_score == pytest.approx(2 / 4)
    assert report.freshness_score == pytest.approx(3 / 4)
    # Four of five claims were tied to at least one row.
    assert report.grounding_score == pytest.approx(4 / 5)


@pytest.mark.unit
def test_report_round_trips_through_json() -> None:
    report = build_report([_verification("SUPPORTED")])
    assert type(report).model_validate_json(report.model_dump_json()) == report


@pytest.mark.unit
def test_claim_is_checked_against_the_rows_it_cites() -> None:
    rows = [_row(450.0, ref="Ecited"), _row(999.0, year=2023, ref="Eother")]
    claim = _claim("Revenue was $450.0 million in FY2024.", ["Ecited"])
    report = verify_claims([claim], rows)
    assert report.claims[0].verdict == "SUPPORTED"
    assert report.claims[0].evidence_refs == ["Ecited"]
    assert report.claims[0].text == claim.text


@pytest.mark.unit
def test_claim_without_refs_uses_every_row() -> None:
    report = verify_claims(
        [_claim("Revenue was $450.0 million in FY2024.")], [_row(450.0)]
    )
    assert report.claims[0].verdict == "SUPPORTED"


@pytest.mark.unit
def test_a_cited_ref_that_matches_no_row_is_unsupported() -> None:
    claim = _claim("Revenue was $450.0 million in FY2024.", ["Emissing"])
    report = verify_claims([claim], [_row(450.0, ref="Eother")])
    assert report.claims[0].verdict == "UNSUPPORTED"


@pytest.mark.unit
def test_claim_ids_are_numbered_in_order() -> None:
    claims = [_claim("Revenue was strong."), _claim("Margins were fine.")]
    report = verify_claims(claims, [_row(450.0)])
    assert [c.claim_id for c in report.claims] == ["claim-1", "claim-2"]


@pytest.mark.unit
def test_non_numeric_claims_are_unverifiable_not_guessed() -> None:
    claim = _claim("Revenue was $450.0 million in FY2024, driven by Services.")
    report = verify_claims([claim], [_row(450.0)])
    assert report.claims[0].verdict == "UNVERIFIABLE"
    assert report.overall_verdict == "UNVERIFIABLE"
