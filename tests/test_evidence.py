"""Resolving cited refs against the rows a run's tools returned (ADR 0030)."""

from typing import Any

import pytest

from recon.runtimes.evidence import RowIndex, resolve_claims
from recon.tools.refs import row_ref

CHUNK = {
    "chunk_id": "FIRM-001-0001-07",
    "company_id": "FIRM-001",
    "form": "10-K",
    "filed": "2024-02-01",
    "accession": "0000000001-24-000001",
    "section": "Item 7",
    "text": "Revenue grew on robot demand.",
    "score": 3.21,
}
FACT = {
    "fiscal_year": 2024,
    "fiscal_period": "FY",
    "period_start": "2024-01-01",
    "period_end": "2024-12-31",
    "concept": "revenue",
    "value": 520.0,
    "unit": "USD_M",
    "form": "10-K",
    "filed": "2025-02-01",
    "accession": "0000000001-25-000001",
}


def _with_ref(tool: str, row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "ref": row_ref(tool, row)}


def _index() -> tuple[RowIndex, str, str]:
    index = RowIndex()
    chunk, fact = (
        _with_ref("search_knowledge", CHUNK),
        _with_ref("get_financial_fact", FACT),
    )
    index.add(
        "search_knowledge", {"query": "revenue"}, {"status": "ok", "data": [chunk]}
    )
    index.add(
        "get_financial_fact",
        {"company_id": "FIRM-001", "concept": "revenue"},
        {"status": "ok", "data": [fact]},
    )
    return index, chunk["ref"], fact["ref"]


def _claim(*refs: str, importance: str = "key") -> dict[str, Any]:
    return {
        "text": "Revenue was 520.",
        "importance": importance,
        "evidence_refs": list(refs),
    }


@pytest.mark.unit
def test_a_ref_a_tool_returned_is_verified_with_the_rows_source() -> None:
    index, chunk_ref, _ = _index()
    claims, evidence, lines = resolve_claims([_claim(chunk_ref)], index)
    assert claims[0].evidence_refs == [chunk_ref]
    item = evidence[0]
    assert item.verified
    assert item.source_type == "filing_text"
    assert (item.company_id, item.form, item.filed, item.section) == (
        "FIRM-001",
        "10-K",
        "2024-02-01",
        "Item 7",
    )
    assert item.accession == "0000000001-24-000001"
    assert item.locator == "FIRM-001-0001-07"
    assert item.excerpt == "Revenue grew on robot demand."
    assert item.retrieval_score == 3.21
    assert item.content_hash and len(item.content_hash) == 64
    assert lines[0].startswith(
        f"[{chunk_ref}] filing_text (FIRM-001, 10-K, filed 2024-02-01"
    )


@pytest.mark.unit
def test_a_fact_is_rendered_from_its_row_and_the_calls_company() -> None:
    index, _, fact_ref = _index()
    _, evidence, _ = resolve_claims([_claim(fact_ref)], index)
    item = evidence[0]
    assert item.source_type == "financial_fact"
    assert item.company_id == "FIRM-001"
    assert item.locator == "revenue 2024 FY"
    assert item.excerpt == "revenue 2024 FY (2024-01-01 to 2024-12-31): 520.0 USD_M"
    assert item.accession == "0000000001-25-000001"


@pytest.mark.unit
def test_a_made_up_ref_stays_unverified_with_nothing_from_the_model() -> None:
    index, _, _ = _index()
    _, evidence, lines = resolve_claims([_claim("Edeadbeef0000")], index)
    assert not evidence[0].verified
    assert evidence[0].source_type == "unknown"
    assert evidence[0].excerpt == ""
    assert "unverified" in lines[0]


@pytest.mark.unit
def test_a_ref_cited_twice_is_one_evidence_item() -> None:
    index, chunk_ref, fact_ref = _index()
    claims, evidence, lines = resolve_claims(
        [
            _claim(chunk_ref, chunk_ref),
            _claim(fact_ref, chunk_ref, importance="supporting"),
        ],
        index,
    )
    assert claims[0].evidence_refs == [chunk_ref]
    assert [item.ref for item in evidence] == [chunk_ref, fact_ref]
    assert len(lines) == 2


@pytest.mark.unit
def test_a_claim_without_refs_is_kept() -> None:
    claims, evidence, _ = resolve_claims([_claim()], RowIndex())
    assert claims[0].evidence_refs == []
    assert evidence == []


@pytest.mark.unit
def test_rows_without_a_ref_and_failed_results_are_not_indexed() -> None:
    index = RowIndex()
    index.add("get_financial_fact", {}, {"status": "ok", "data": [FACT]})
    index.add("get_financial_fact", {}, {"status": "unavailable", "data": []})
    index.add("get_financial_fact", {}, {"status": "ok"})
    assert index.rows == {}


@pytest.mark.unit
def test_merging_indexes_keeps_every_workers_rows() -> None:
    first, chunk_ref, fact_ref = _index()
    second = RowIndex()
    second.merge(first)
    assert set(second.rows) == {chunk_ref, fact_ref}
