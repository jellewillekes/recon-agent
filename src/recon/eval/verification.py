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
    claims: list[Claim],
    evidence: list[Row],
    tolerance: float = 0.0,
    non_fact_refs: set[str] | None = None,
) -> VerificationReport:
    """Check each claim against the rows it cites, or against all of `evidence`
    when it cites none. A cited ref that matches no row leaves the claim with
    nothing to rest on, so it comes back UNSUPPORTED. A claim with a figure is
    UNSUPPORTED as soon as one of its refs matches nothing: neither a row nor
    one of `non_fact_refs`, the real refs that aren't fact rows."""
    verified = [
        _verify_one(f"claim-{n}", claim, evidence, tolerance, non_fact_refs or set())
        for n, claim in enumerate(claims, start=1)
    ]
    return build_report(verified)


def _verify_one(
    claim_id: str,
    claim: Claim,
    evidence: list[Row],
    tolerance: float,
    non_fact_refs: set[str],
) -> ClaimVerification:
    if claim.figure is not None:
        known = {row.get("ref") for row in evidence} | non_fact_refs
        missing = [ref for ref in claim.evidence_refs if ref not in known]
        if missing:
            return ClaimVerification(
                claim_id=claim_id,
                text=claim.text,
                verdict="UNSUPPORTED",
                evidence_refs=[],
                claimed_value=None,
                recomputed_value=None,
                tolerance=None,
                reasoning=f"The figure cites {missing[0]}, which matches no row.",
            )
    rows = _rows_for(claim, evidence)
    return verify_claim(claim_id, claim.text, rows, tolerance, claim.figure)


def _rows_for(claim: Claim, evidence: list[Row]) -> list[Row]:
    """The rows a claim is checked against. A figure is checked only against
    the rows it cites, in the order it cites them: a ratio's numerator first.
    A text claim keeps the evidence order, which decides between versions
    filed the same day."""
    if not claim.evidence_refs:
        return [] if claim.figure is not None else evidence
    order = {ref: n for n, ref in enumerate(claim.evidence_refs)}
    cited = [row for row in evidence if row.get("ref") in order]
    if claim.figure is None:
        return cited
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
