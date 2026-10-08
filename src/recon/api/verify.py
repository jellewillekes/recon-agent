"""`POST /verify`: check an answer's claims against the tool rows they cite
(ADR 0035). Any agent can send its result here. It never calls a model, so it
spends no credit and needs no Postgres.
"""

from fastapi import APIRouter

from recon.api.schemas import VerifyRequest
from recon.contracts import VerificationReport
from recon.eval.verification import verify_claims

router = APIRouter()


@router.post("/verify")
async def verify(request: VerifyRequest) -> VerificationReport:
    """A verdict for each claim, and what they add up to. Claims the numeric
    verifier can't read come back UNVERIFIABLE; an answer with no claims is
    UNVERIFIABLE as a whole."""
    return verify_claims(request.claims, request.evidence)
