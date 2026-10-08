"""Tests for the labelled claim set loader (src/recon/eval/claim_set.py)."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from recon.eval.claim_set import (
    VERDICTS,
    check_coverage,
    load_claim_set,
    verdict_counts,
)

ROW = {
    "ref": "E00000000001",
    "concept": "revenue",
    "fiscal_year": 2024,
    "fiscal_period": "FY",
    "value": 1.0,
    "unit": "USD_M",
}


def _claim(claim_id: str = "c1", **overrides: Any) -> dict[str, Any]:
    claim = {
        "id": claim_id,
        "text": "Revenue was $1.0 million in FY2024.",
        "scope": "numeric",
        "rowset": "rs",
        "expected_verdict": "SUPPORTED",
        "reason": "Matches the row.",
    }
    return {**claim, **overrides}


def _write(tmp_path: Path, claims: list[dict[str, Any]]) -> Path:
    path = tmp_path / "claims.yaml"
    path.write_text(yaml.safe_dump({"rowsets": {"rs": [ROW]}, "claims": claims}))
    return path


def test_default_set_loads_with_resolved_rows() -> None:
    claims = load_claim_set()
    assert claims
    assert all(
        claim.rows or claim.expected_verdict == "UNSUPPORTED" for claim in claims
    )


def test_default_set_covers_every_verdict() -> None:
    check_coverage(load_claim_set())


def test_ids_are_unique_in_the_default_set() -> None:
    ids = [claim.id for claim in load_claim_set()]
    assert len(ids) == len(set(ids))


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, [_claim("same"), _claim("same")])
    with pytest.raises(ValueError, match="duplicate"):
        load_claim_set(path)


def test_unknown_rowset_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, [_claim(rowset="missing")])
    with pytest.raises(ValueError, match="missing"):
        load_claim_set(path)


def test_unknown_verdict_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, [_claim(expected_verdict="MAYBE")])
    with pytest.raises(ValueError):
        load_claim_set(path)


def test_claim_without_reason_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, [_claim(reason="")])
    with pytest.raises(ValueError):
        load_claim_set(path)


def test_coverage_names_the_short_verdicts(tmp_path: Path) -> None:
    claims = load_claim_set(_write(tmp_path, [_claim("only-one")]))
    with pytest.raises(ValueError, match="CONTRADICTED"):
        check_coverage(claims)


def test_verdict_counts_include_zeros(tmp_path: Path) -> None:
    claims = load_claim_set(_write(tmp_path, [_claim()]))
    counts = verdict_counts(claims)
    assert set(counts) == set(VERDICTS)
    assert counts["SUPPORTED"] == 1
    assert counts["STALE"] == 0
