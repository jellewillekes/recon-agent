"""Load `config/thresholds.yaml`: the gate's limits, the correct-answer cutoff
and the minimums the committed baseline must reach. See docs/contracts.md §9.
"""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_THRESHOLDS_PATH = Path("config/thresholds.yaml")


class GateThresholds(BaseModel):
    """How far a comparable candidate may fall behind the baseline."""

    model_config = ConfigDict(extra="forbid")

    answer_score_max_relative_drop: float = Field(ge=0)
    cost_max_relative_rise: float = Field(ge=0)


class Thresholds(BaseModel):
    """Everything in `config/thresholds.yaml`."""

    model_config = ConfigDict(extra="forbid")

    gate: GateThresholds
    correct_answer_score: float | None = Field(default=None, ge=0, le=1)
    baseline_minimums: dict[str, float] = {}


def load_thresholds(path: Path = DEFAULT_THRESHOLDS_PATH) -> Thresholds:
    """Parse and validate the thresholds file. A typo'd key is an error."""
    return Thresholds.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
