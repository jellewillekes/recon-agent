"""The gate's claim-level check (#133, ADR 0035).

An answer can keep its score while more of its claims turn out unsupported or
contradicted. This compares how a candidate's claims were verified with the
baseline's. Runs that never verified claims are "not measured": they neither
pass nor fail here.
"""

from recon.contracts import VERDICTS, CaseScore, ClaimVerification, EvalRun
from recon.eval.thresholds import GateThresholds

_BAD = ("UNSUPPORTED", "CONTRADICTED")
_QUOTED_CLAIMS = 3


def measured(run: EvalRun) -> bool:
    """Whether any case of `run` has its claims verified."""
    return any(case.verifications is not None for case in run.case_scores)


def _verified(run: EvalRun) -> list[ClaimVerification]:
    return [v for case in run.case_scores for v in case.verifications or []]


def bad_rate(run: EvalRun) -> float | None:
    """Unsupported and contradicted claims as a share of the claims the
    verifier could read. None when it read none."""
    checked = [v for v in _verified(run) if v.verdict != "UNVERIFIABLE"]
    if not checked:
        return None
    return sum(1 for v in checked if v.verdict in _BAD) / len(checked)


def verdict_counts(run: EvalRun) -> dict[str, int] | None:
    """How many of the run's claims got each verdict. None when the run
    didn't verify claims."""
    if not measured(run):
        return None
    counts: dict[str, int] = dict.fromkeys(VERDICTS, 0)
    for verification in _verified(run):
        counts[verification.verdict] += 1
    return counts


def _contradicted(case: CaseScore | None) -> list[ClaimVerification]:
    return (
        [v for v in (case.verifications or []) if v.verdict == "CONTRADICTED"]
        if case
        else []
    )


def version_failures(candidate: EvalRun, baseline: EvalRun) -> list[str]:
    """Why the two runs' claim verdicts can't be compared, if they can't."""
    if not measured(baseline):
        return []
    if not measured(candidate):
        return [
            (
                "the baseline verified its claims and the candidate didn't. "
                "Verify the candidate's claims, or regenerate the baseline "
                "through an explicit PR."
            )
        ]
    if baseline.verifier_version != candidate.verifier_version:
        return [
            (
                f"verifier_version differs: baseline {baseline.verifier_version!r}, "
                f"candidate {candidate.verifier_version!r}. Claim verdicts aren't "
                "comparable. Regenerate the baseline through an explicit PR."
            )
        ]
    return []


def claim_failures(
    candidate: EvalRun, baseline: EvalRun, limits: GateThresholds
) -> list[str]:
    """The claim rules `candidate` fails against `baseline`; empty if none.

    Fails when the unsupported-or-contradicted share rises by more than
    `limits.claim_bad_rate_noise_band`, and for any case that completed in the
    baseline and now has more contradicted claims. Call after
    `version_failures` comes back empty.
    """
    if not measured(baseline):
        return []
    failures: list[str] = []
    before, after = bad_rate(baseline), bad_rate(candidate)
    if (
        before is not None
        and after is not None
        and after - before > limits.claim_bad_rate_noise_band + 1e-9
    ):
        failures.append(
            "unsupported and contradicted claims rose from "
            f"{before:.1%} to {after:.1%} of the claims checked "
            f"(allowed rise {limits.claim_bad_rate_noise_band:.1%}); cases: "
            + ", ".join(_cases_with_more_bad_claims(candidate, baseline))
        )
    failures.extend(_new_contradictions(candidate, baseline))
    return failures


def _cases_with_more_bad_claims(candidate: EvalRun, baseline: EvalRun) -> list[str]:
    by_id = {case.case_id: case for case in baseline.case_scores}

    def bad(case: CaseScore | None) -> int:
        return (
            sum(1 for v in (case.verifications or []) if v.verdict in _BAD)
            if case
            else 0
        )

    named = [
        repr(case.case_id)
        for case in candidate.case_scores
        if bad(case) > bad(by_id.get(case.case_id))
    ]
    return named or ["none singled out"]


def _new_contradictions(candidate: EvalRun, baseline: EvalRun) -> list[str]:
    by_id = {case.case_id: case for case in baseline.case_scores}
    failures = []
    for case in candidate.case_scores:
        before = by_id.get(case.case_id)
        now = _contradicted(case)
        if before is None or not before.task_completion:
            continue
        if before.verifications is None or len(now) <= len(_contradicted(before)):
            continue
        quoted = "; ".join(f'"{v.text}"' for v in now[:_QUOTED_CLAIMS])
        failures.append(
            f"case {case.case_id!r} completed in the baseline and now has "
            f"{len(now)} contradicted claim(s): {quoted}"
        )
    return failures
