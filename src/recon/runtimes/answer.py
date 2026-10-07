"""The final answer's schema and its validation, shared by single mode and
multi mode's synthesis (ADR 0030).

The model answers with claims that cite row refs, not evidence strings. The
refs are resolved against the rows the run's tools returned
(`runtimes/evidence.py`).
"""

from dataclasses import dataclass
from typing import Any, Literal, cast, get_args

from recon.contracts import Claim, Evidence
from recon.runtimes.evidence import RowIndex, resolve_claims

Confidence = Literal["high", "medium", "low"]
_CONFIDENCE_VALUES = set(get_args(Confidence))

CLAIMS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "importance": {"type": "string", "enum": ["key", "supporting"]},
            "evidence_refs": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["text", "importance", "evidence_refs"],
        "additionalProperties": False,
    },
}

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "claims": CLAIMS_SCHEMA,
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        },
        "required": ["answer", "claims", "confidence"],
        "additionalProperties": False,
    },
}


@dataclass
class Answer:
    """A validated final answer with its claims resolved."""

    answer: str
    confidence: Confidence
    claims: list[Claim]
    evidence_items: list[Evidence]
    evidence: list[str]


def validate_answer(structured: dict[str, Any], rows: RowIndex) -> Answer:
    """Check `structured` against `ANSWER_SCHEMA`'s essentials and resolve its
    claims. Missing claims count as none: the answer is kept, uncited."""
    answer, confidence = structured["answer"], structured["confidence"]
    if confidence not in _CONFIDENCE_VALUES:
        raise RuntimeError(
            f"agent_sdk run returned an invalid confidence: {confidence!r}."
        )
    claims, evidence_items, evidence = resolve_claims(
        structured.get("claims") or [], rows
    )
    return Answer(
        answer=answer,
        confidence=cast(Confidence, confidence),
        claims=claims,
        evidence_items=evidence_items,
        evidence=evidence,
    )
