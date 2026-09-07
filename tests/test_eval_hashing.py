"""Tests for `eval/hashing.py`."""

import hashlib
from pathlib import Path

import pytest

from recon.eval import hashing


@pytest.mark.unit
def test_compute_prompt_hashes_keys_by_stem_and_skips_non_md(tmp_path: Path) -> None:
    (tmp_path / "investigator.md").write_text("investigator prompt", encoding="utf-8")
    (tmp_path / "critic.md").write_text("critic prompt", encoding="utf-8")
    (tmp_path / ".gitkeep").write_text("", encoding="utf-8")

    hashes = hashing.compute_prompt_hashes(tmp_path)

    assert set(hashes) == {"investigator", "critic"}
    assert hashes["investigator"] == hashlib.sha256(b"investigator prompt").hexdigest()


@pytest.mark.unit
def test_compute_prompt_hashes_changes_when_file_changes(tmp_path: Path) -> None:
    prompt_path = tmp_path / "investigator.md"
    prompt_path.write_text("v1", encoding="utf-8")
    first = hashing.compute_prompt_hashes(tmp_path)["investigator"]

    prompt_path.write_text("v2", encoding="utf-8")
    second = hashing.compute_prompt_hashes(tmp_path)["investigator"]

    assert first != second


@pytest.mark.unit
def test_compute_model_config_hash_matches_sha256_of_file(tmp_path: Path) -> None:
    config_path = tmp_path / "models.yaml"
    config_path.write_text("investigator:\n  model: x\n", encoding="utf-8")

    result = hashing.compute_model_config_hash(config_path)

    assert result == hashlib.sha256(config_path.read_bytes()).hexdigest()
