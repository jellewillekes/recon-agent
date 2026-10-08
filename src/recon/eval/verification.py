"""Verify the claims of one answer and total the result (#133, ADR 0035).

`verify_claims` checks each claim against the evidence rows it cites, with the
numeric verifier (ADR 0034). `build_report` turns per-claim verdicts into a
`VerificationReport`. Neither calls a model.
"""

from recon.contracts import (
    VERDICTS,
    Claim,
    ClaimVerification,
    Verdict,
    VerificationReport,
)
from recon.eval.claim_reader import Row
from recon.eval.claim_verifier import verify_claim

# Worst first. The first of these present in an answer is its overall verdict.
_WORST_FIRST: tuple[Verdict, ...] = ("CONTRADICTED", "STALE", "UNSUPPORTED")


def verify_claims(
    claims: list[Claim], evidence: list[Row], tolerance: float = 0.0
) -> VerificationReport:
    """Check each claim against the rows it cites, or against all of `evidence`
    when it cites none. A cited ref that matches no row leaves the claim with
    nothing to rest on, so it comes back UNSUPPORTED."""
    verified = [
        verify_claim(
            f"claim-{n}",
            claim.text,
            _rows_for(claim, evidence),
            tolerance,
            claim.figure,
        )
        for n, claim in enumerate(claims, start=1)
    ]
    return build_report(verified)


def _rows_for(claim: Claim, evidence: list[Row]) -> list[Row]:
    """The rows a claim is checked against. A figure is checked only against
    the rows it cites, in the order it cites them: a ratio's numerator first."""
    if not claim.evidence_refs:
        return [] if claim.figure is not None else evidence
    order = {ref: n for n, ref in enumerate(claim.evidence_refs)}
    cited = [row for row in evidence if row.get("ref") in order]
    return sorted(cited, key=lambda row: order[row["ref"]])


def build_report(claims: list[ClaimVerification]) -> VerificationReport:
    """Count the verdicts and score them.

    - `correctness_score`: SUPPORTED claims among those the verifier could read.
    - `freshness_score`: readable claims that are not STALE.
    - `grounding_score`: claims tied to at least one row, among all claims.
    """
    counts: dict[str, int] = dict.fromkeys(VERDICTS, 0)
    for claim in claims:
        counts[claim.verdict] += 1
    checked = len(claims) - counts["UNVERIFIABLE"]
    return VerificationReport(
        claims=claims,
        counts=counts,
        grounding_score=_share(sum(1 for c in claims if c.evidence_refs), len(claims)),
        correctness_score=_share(counts["SUPPORTED"], checked),
        freshness_score=_share(checked - counts["STALE"], checked),
        overall_verdict=_overall(counts),
    )


def _share(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def _overall(counts: dict[str, int]) -> Verdict:
    for verdict in _WORST_FIRST:
        if counts[verdict]:
            return verdict
    if counts["UNVERIFIABLE"] == sum(counts.values()):
        return "UNVERIFIABLE"
    if counts["UNVERIFIABLE"] or counts["PARTIALLY_SUPPORTED"]:
        return "PARTIALLY_SUPPORTED"
    return "SUPPORTED"
