"""The labelled claim set: claims with the tool rows they cite and the verdict
a correct verifier gives (#137, ADR 0034).

`evals/verification-claims.yaml` holds named row sets and the claims that use
them. Loading resolves each claim's `rowset` into its rows, so a claim carries
everything the verifier needs and no database is involved.
"""

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from recon.eval.claim_verifier import VERDICTS, Verdict

DEFAULT_CLAIM_SET_PATH = Path("evals/verification-claims.yaml")
MIN_PER_VERDICT = 5


class LabelledClaim(BaseModel):
    """One claim, its rows and the verdict a correct verifier returns.

    `scope` says who is expected to reach that verdict. `numeric` is the
    numeric verifier; `llm` needs judgement about causes or wording, and the
    numeric verifier must return UNVERIFIABLE there instead of guessing.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    scope: Literal["numeric", "llm"]
    rows: list[dict[str, Any]]
    expected_verdict: Verdict
    reason: str = Field(min_length=1)


def load_claim_set(path: Path = DEFAULT_CLAIM_SET_PATH) -> list[LabelledClaim]:
    """Load the claims in `path`, each with its row set resolved.

    Raises ValueError for a duplicate id, a row set that isn't defined, or a
    claim that doesn't validate, naming the claim and what to fix.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    rowsets: dict[str, list[dict[str, Any]]] = raw.get("rowsets", {})
    claims: list[LabelledClaim] = []
    seen: set[str] = set()
    for entry in raw.get("claims", []):
        claim_id = entry.get("id", "<no id>")
        if claim_id in seen:
            raise ValueError(
                f"{path}: duplicate claim id {claim_id!r}; ids are unique."
            )
        seen.add(claim_id)
        name = entry.get("rowset")
        if name not in rowsets:
            raise ValueError(
                f"{path}: claim {claim_id!r} uses rowset {name!r}, which is not "
                f"defined. Defined: {sorted(rowsets)}."
            )
        body = {key: value for key, value in entry.items() if key != "rowset"}
        try:
            claims.append(LabelledClaim(rows=rowsets[name], **body))
        except ValidationError as error:
            raise ValueError(
                f"{path}: claim {claim_id!r} is invalid: {error}"
            ) from error
    return claims


def verdict_counts(claims: list[LabelledClaim]) -> dict[str, int]:
    """How many claims carry each verdict, with zero for a verdict none carry."""
    counts: dict[str, int] = dict.fromkeys(VERDICTS, 0)
    for claim in claims:
        counts[claim.expected_verdict] += 1
    return counts


def check_coverage(claims: list[LabelledClaim], minimum: int = MIN_PER_VERDICT) -> None:
    """Raise ValueError naming every verdict with fewer than `minimum` claims."""
    short = {
        verdict: count
        for verdict, count in verdict_counts(claims).items()
        if count < minimum
    }
    if short:
        raise ValueError(
            f"Each verdict needs at least {minimum} labelled claims. "
            f"Short: {short}. Add claims to the set."
        )
