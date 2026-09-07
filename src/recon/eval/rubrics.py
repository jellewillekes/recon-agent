"""Dimension-level rubrics: docs/contracts.md §8.

One YAML per dimension in `config/rubrics/`. Assertions, not free-form
judgement — each is a discrete, checkable statement rather than an open essay
grade.
"""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

DEFAULT_RUBRICS_DIR = Path("config/rubrics")


class Assertion(BaseModel):
    """One checkable statement within a dimension."""

    id: str
    text: str
    score_if_true: float = Field(ge=0.0, le=1.0)


class Rubric(BaseModel):
    """One dimension's worth of assertions, loaded from `config/rubrics/<dimension>.yaml`."""

    dimension: str
    version: int
    weight: float = Field(ge=0.0, le=1.0)
    assertions: list[Assertion]


def load_rubrics(rubrics_dir: Path = DEFAULT_RUBRICS_DIR) -> dict[str, Rubric]:
    """Load every `*.yaml` in `rubrics_dir`, keyed by `Rubric.dimension`."""
    rubrics: dict[str, Rubric] = {}
    for path in sorted(rubrics_dir.glob("*.yaml")):
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
        rubric = Rubric.model_validate(data)
        rubrics[rubric.dimension] = rubric
    return rubrics
