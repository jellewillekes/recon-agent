"""Resolve the refs an answer cites against the rows its tools returned
(ADR 0030).

The runtime collects every row with a `ref` while the run happens
(`RowIndex.add`). After the model answers, `resolve_claims` turns its claims
into `Claim`s and `Evidence`. A cited ref that matches a collected row is
verified, and the evidence is rendered from that row. Nothing the model wrote
ends up in an excerpt.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from recon.contracts import Claim, Evidence

# A filing-text chunk is kept whole in `excerpt` up to this length.
_MAX_EXCERPT_CHARS = 1500


@dataclass
class _Row:
    tool: str
    arguments: dict[str, Any]
    row: dict[str, Any]


@dataclass
class RowIndex:
    """Every row with a `ref` the tools returned in one run, by ref."""

    rows: dict[str, _Row] = field(default_factory=dict)

    def add(
        self, tool: str, arguments: dict[str, Any], payload: dict[str, Any]
    ) -> None:
        """Index the rows of one tool result. Rows without a string `ref`
        (an older server, the write tool) are skipped."""
        data = payload.get("data")
        if not isinstance(data, list):
            return
        for row in data:
            if isinstance(row, dict) and isinstance(row.get("ref"), str):
                self.rows[row["ref"]] = _Row(tool, dict(arguments), row)

    def merge(self, other: "RowIndex") -> None:
        """Add another index's rows, e.g. a multi-mode worker's."""
        self.rows.update(other.rows)


def _content_hash(row: dict[str, Any]) -> str:
    content = {key: value for key, value in row.items() if key != "ref"}
    canonical = json.dumps(content, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _filing_text(found: _Row) -> dict[str, Any]:
    row = found.row
    score = row.get("score")
    return {
        "source_type": "filing_text",
        "company_id": _text(row.get("company_id")),
        "form": _text(row.get("form")),
        "filed": _text(row.get("filed")),
        "accession": _text(row.get("accession")),
        "section": _text(row.get("section")),
        "locator": _text(row.get("chunk_id")),
        "excerpt": str(row.get("text", ""))[:_MAX_EXCERPT_CHARS],
        "retrieval_score": float(score) if isinstance(score, int | float) else None,
    }


def _financial_fact(found: _Row) -> dict[str, Any]:
    row = found.row
    period = " ".join(
        str(part) for part in (row.get("fiscal_year"), row.get("fiscal_period")) if part
    )
    dates = f" ({row.get('period_start')} to {row.get('period_end')})"
    concept = row.get("concept", "")
    return {
        "source_type": "financial_fact",
        "company_id": _text(found.arguments.get("company_id")),
        "form": _text(row.get("form")),
        "filed": _text(row.get("filed")),
        "accession": _text(row.get("accession")),
        "locator": f"{concept} {period}".strip(),
        "excerpt": f"{concept} {period}{dates}: {row.get('value')} {row.get('unit', '')}".strip(),
    }


def _filing(found: _Row) -> dict[str, Any]:
    row = found.row
    return {
        "source_type": "filing",
        "company_id": _text(found.arguments.get("company_id")),
        "form": _text(row.get("form_type")),
        "filed": _text(row.get("filed_date")),
        "accession": _text(row.get("accession")),
        "locator": _text(row.get("primary_document")),
        "excerpt": str(row.get("summary_text") or ""),
    }


def _company(found: _Row) -> dict[str, Any]:
    row = found.row
    return {
        "source_type": "company",
        "company_id": _text(row.get("company_id")),
        "locator": _text(row.get("company_id")),
        "excerpt": f"{row.get('company_id')}: {row.get('name')} ({row.get('sector')})",
    }


def _concept(found: _Row) -> dict[str, Any]:
    row = found.row
    return {
        "source_type": "concept",
        "company_id": _text(found.arguments.get("company_id")),
        "locator": _text(row.get("concept")),
        "excerpt": f"{row.get('concept')}: {row.get('label')} ({row.get('units')})",
    }


_RENDERERS: dict[str, Callable[[_Row], dict[str, Any]]] = {
    "search_knowledge": _filing_text,
    "get_financial_fact": _financial_fact,
    "search_filings": _filing,
    "list_companies": _company,
    "list_financial_concepts": _concept,
}


def evidence_for(ref: str, index: RowIndex) -> Evidence:
    """The evidence behind `ref`: rendered from its row when a tool returned
    it in this run, otherwise unverified with only the ref."""
    found = index.rows.get(ref)
    renderer = _RENDERERS.get(found.tool) if found is not None else None
    if found is None or renderer is None:
        return Evidence(ref=ref, verified=False, source_type="unknown")
    return Evidence(
        ref=ref,
        verified=True,
        tool=found.tool,
        content_hash=_content_hash(found.row),
        **renderer(found),
    )


def evidence_line(evidence: Evidence) -> str:
    """One line for `AgentResult.evidence`, which the judges read."""
    if not evidence.verified:
        return f"[{evidence.ref}] unverified: no tool returned this ref in this run"
    source = ", ".join(
        part
        for part in (
            evidence.company_id,
            evidence.form,
            f"filed {evidence.filed}" if evidence.filed else None,
            f"accession {evidence.accession}" if evidence.accession else None,
            evidence.section,
        )
        if part
    )
    return f"[{evidence.ref}] {evidence.source_type} ({source}): {evidence.excerpt}"


def resolve_claims(
    raw_claims: list[dict[str, Any]], index: RowIndex
) -> tuple[list[Claim], list[Evidence], list[str]]:
    """Claims as the answer schema returns them, resolved against `index`.

    Returns the claims, the evidence for every cited ref (each once, in the
    order first cited), and one `AgentResult.evidence` line per evidence.
    """
    claims: list[Claim] = []
    evidence: dict[str, Evidence] = {}
    for raw in raw_claims:
        refs = list(dict.fromkeys(str(ref) for ref in raw.get("evidence_refs", [])))
        claims.append(
            Claim(text=raw["text"], importance=raw["importance"], evidence_refs=refs)
        )
        for ref in refs:
            if ref not in evidence:
                evidence[ref] = evidence_for(ref, index)
    items = list(evidence.values())
    return claims, items, [evidence_line(item) for item in items]


def worker_report(worker: str, findings: str, refs: list[str], rows: RowIndex) -> str:
    """A multi-mode worker's findings for the synthesis, with each cited ref
    resolved, so the supervisor reads tool rows rather than the worker's
    paraphrase of them. Shared by both runtimes' multi mode."""
    lines = [evidence_line(evidence_for(ref, rows)) for ref in dict.fromkeys(refs)]
    return f"[{worker}] findings: {findings}\nevidence:\n" + "\n".join(lines)
