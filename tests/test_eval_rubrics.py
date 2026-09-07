"""Tests for `eval/rubrics.py`."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from recon.eval.rubrics import Assertion, Rubric, load_rubrics

REPO_RUBRICS_DIR = Path(__file__).parent.parent / "config" / "rubrics"


@pytest.mark.unit
def test_load_rubrics_reads_every_yaml_keyed_by_dimension() -> None:
    rubrics = load_rubrics(REPO_RUBRICS_DIR)

    assert set(rubrics) == {
        "answer_correctness",
        "evidence_grounding",
        "tool_efficiency",
    }
    for rubric in rubrics.values():
        assert isinstance(rubric, Rubric)


@pytest.mark.unit
def test_answer_correctness_has_no_static_assertions() -> None:
    """docs/data-sources.md: this dimension is expanded per-case by judge.py
    from the dataset's own rubric, not from static YAML text."""
    rubrics = load_rubrics(REPO_RUBRICS_DIR)
    assert rubrics["answer_correctness"].assertions == []


@pytest.mark.unit
def test_evidence_grounding_and_tool_efficiency_have_assertions() -> None:
    rubrics = load_rubrics(REPO_RUBRICS_DIR)
    assert len(rubrics["evidence_grounding"].assertions) > 0
    assert len(rubrics["tool_efficiency"].assertions) > 0


@pytest.mark.unit
def test_load_rubrics_from_directory(tmp_path: Path) -> None:
    (tmp_path / "custom.yaml").write_text(
        "dimension: custom\nversion: 1\nweight: 0.5\n"
        "assertions:\n  - id: a1\n    text: 'something'\n    score_if_true: 1.0\n",
        encoding="utf-8",
    )

    rubrics = load_rubrics(tmp_path)

    assert rubrics["custom"].version == 1
    assert rubrics["custom"].assertions[0].id == "a1"


@pytest.mark.unit
def test_assertion_score_if_true_must_be_in_unit_range() -> None:
    with pytest.raises(ValidationError):
        Assertion(id="x", text="t", score_if_true=1.5)


@pytest.mark.unit
def test_rubric_weight_must_be_in_unit_range() -> None:
    with pytest.raises(ValidationError):
        Rubric(dimension="d", version=1, weight=-0.1, assertions=[])
