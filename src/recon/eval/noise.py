"""Run-to-run noise, measured from stored repeat runs (#135, ADR 0036).

A repeat is a run with the same settings and the same cases as another. How far
their scores differ is the noise the promotion gate's band has to allow for
(ADR 0028). A confidence interval can't give that: it describes other cases,
not a rerun of these.
"""

import json
import math
import statistics
from typing import Any

from pydantic import BaseModel

from recon.contracts import EvalRun
from recon.eval.intervals import t_critical


class NoiseEstimate(BaseModel):
    """How far two runs of the same settings differ in answer score mean."""

    runs: int
    cases: int
    # Standard deviation of the difference between two runs' answer score means.
    sd_of_difference: float
    # Differences larger than this are unlikely (95%) to be noise alone.
    band_95: float


def settings_key(run: EvalRun) -> tuple[Any, ...]:
    """What must match for two runs to be repeats of each other."""
    return (
        run.dataset,
        run.rubric_version,
        run.tool_data_snapshot,
        run.model_config_hash,
        json.dumps(run.prompt_hashes, sort_keys=True),
        run.runtime,
        run.mode,
        run.routing,
        frozenset(score.case_id for score in run.case_scores),
    )


def repeat_groups(runs: list[EvalRun]) -> list[list[EvalRun]]:
    """Groups of two or more runs that are repeats of each other, in the order
    their first run appears in `runs`."""
    groups: dict[tuple[Any, ...], list[EvalRun]] = {}
    for run in runs:
        groups.setdefault(settings_key(run), []).append(run)
    return [group for group in groups.values() if len(group) >= 2]


def estimate_noise(runs: list[EvalRun]) -> NoiseEstimate:
    """The noise in answer score mean between repeats of one setting.

    Each case's scores across the runs give a run-to-run variance; their mean
    over cases is the within-case variance `s2`. The difference of two runs'
    means then has sd `sqrt(2 * s2 / cases)`, and the band is that times the
    t value for `cases * (runs - 1)` degrees of freedom. Raises ValueError for
    fewer than two runs.
    """
    if len(runs) < 2:
        raise ValueError("Measuring noise needs at least two runs of one setting.")
    by_case = [
        [
            score.answer_score
            for run in runs
            for score in run.case_scores
            if score.case_id == case_id
        ]
        for case_id in sorted({s.case_id for s in runs[0].case_scores})
    ]
    cases = len(by_case)
    within = statistics.fmean(statistics.variance(scores) for scores in by_case)
    sd = math.sqrt(2 * within / cases)
    return NoiseEstimate(
        runs=len(runs),
        cases=cases,
        sd_of_difference=sd,
        band_95=t_critical(cases * (len(runs) - 1)) * sd,
    )
