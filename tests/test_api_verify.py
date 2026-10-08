"""`POST /verify`: per-claim verdicts for an answer's claims and evidence rows.
Fixtures only; no model is called."""

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from recon.api import main as api_main

pytestmark = [pytest.mark.unit, pytest.mark.anyio]

ROW: dict[str, Any] = {
    "ref": "E00000000001",
    "concept": "revenue",
    "fiscal_year": 2024,
    "fiscal_period": "FY",
    "value": 450.0,
    "unit": "USD_M",
    "filed": "2025-02-14",
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _post(body: dict[str, Any]) -> Any:
    transport = ASGITransport(app=api_main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/verify", json=body)


async def test_verifies_each_claim_against_its_evidence() -> None:
    resp = await _post(
        {
            "claims": [
                {
                    "text": "Revenue was $450.0 million in FY2024.",
                    "importance": "key",
                    "evidence_refs": ["E00000000001"],
                },
                {
                    "text": "Revenue was $480.0 million in FY2024.",
                    "importance": "key",
                    "evidence_refs": ["E00000000001"],
                },
            ],
            "evidence": [ROW],
        }
    )
    assert resp.status_code == 200
    report = resp.json()
    assert [c["verdict"] for c in report["claims"]] == ["SUPPORTED", "CONTRADICTED"]
    assert report["overall_verdict"] == "CONTRADICTED"
    assert report["counts"]["SUPPORTED"] == 1
    assert report["correctness_score"] == pytest.approx(0.5)


async def test_without_claims_the_answer_is_unverifiable() -> None:
    resp = await _post({"evidence": [ROW]})
    assert resp.status_code == 200
    report = resp.json()
    assert report["claims"] == []
    assert report["overall_verdict"] == "UNVERIFIABLE"


async def test_an_invalid_claim_names_the_field() -> None:
    resp = await _post({"claims": [{"text": "x", "importance": "huge"}]})
    assert resp.status_code == 400
    assert "importance" in resp.text


async def test_the_request_id_is_echoed() -> None:
    transport = ASGITransport(app=api_main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/verify", json={}, headers={"X-Request-ID": "abc"})
    assert resp.headers["X-Request-ID"] == "abc"


async def test_too_many_claims_are_refused() -> None:
    claim = {"text": "Revenue was strong.", "importance": "key", "evidence_refs": []}
    resp = await _post({"claims": [claim] * 101})
    assert resp.status_code == 400
