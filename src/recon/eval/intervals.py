"""95% intervals on a run's headline numbers (#135, ADR 0036).

An interval here says how far a score could move on other cases than the ones
run. It does not say how much a rerun of the same cases moves; that is run-to-run
noise, measured from repeat runs (`eval/noise.py`).

- Task completion is a rate of whole cases: the Wilson score interval.
- The answer score mean uses a Student t interval over per-case scores, clipped
  to the score range. A bootstrap would be too narrow at a handful of cases.
- Cost per correct answer is a ratio with no simple formula: a seeded
  bootstrap over cases.

Everything is standard library, and the same input gives the same interval.
"""

import math
import random
import statistics

from pydantic import BaseModel

from recon.contracts import CaseScore, EvalRun

Z_95 = 1.959964
# Two-sided 95% critical values of Student's t, by degrees of freedom.
_T_95 = (
    12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
    2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
    2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
)  # fmt: skip
_BOOTSTRAP_RESAMPLES = 4000
_BOOTSTRAP_SEED = 0
_TAIL = 0.025


class Interval(BaseModel):
    """A 95% interval."""

    low: float
    high: float


def t_critical(df: int) -> float:
    """Two-sided 95% critical value of Student's t with `df` degrees of freedom.
    Past 30 it is 2.0 (a little wide) up to 120, then the normal 1.96."""
    if df < 1:
        raise ValueError(f"Degrees of freedom must be at least 1, got {df}.")
    if df <= len(_T_95):
        return _T_95[df - 1]
    return 2.0 if df <= 120 else 1.96


def wilson_interval(successes: int, n: int) -> Interval | None:
    """Wilson score interval for `successes` out of `n`. None when `n` is 0."""
    if not 0 <= successes <= n:
        raise ValueError(f"successes must be between 0 and n, got {successes} of {n}.")
    if n == 0:
        return None
    p = successes / n
    z2 = Z_95**2
    centre = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = Z_95 * math.sqrt(p * (1 - p) / n + z2 / (4 * n**2)) / (1 + z2 / n)
    return Interval(low=max(0.0, centre - half), high=min(1.0, centre + half))


def mean_interval(
    values: list[float], bounds: tuple[float, float] | None = None
) -> Interval | None:
    """Student t interval for the mean of `values`, clipped to `bounds`. None
    for fewer than two values, since there is no spread to estimate."""
    n = len(values)
    if n < 2:
        return None
    mean = statistics.fmean(values)
    half = t_critical(n - 1) * statistics.stdev(values) / math.sqrt(n)
    low, high = mean - half, mean + half
    if bounds is not None:
        low, high = max(bounds[0], low), min(bounds[1], high)
    return Interval(low=low, high=high)


def cost_per_correct_interval(
    case_scores: list[CaseScore], correct_answer_score: float
) -> Interval | None:
    """Bootstrap interval for total cost divided by correct cases. Resamples
    with no correct case have no ratio and are left out. None when the run has
    no correct case, or when most resamples have none."""
    n = len(case_scores)
    if n == 0 or not any(s.answer_score >= correct_answer_score for s in case_scores):
        return None
    rng = random.Random(_BOOTSTRAP_SEED)
    ratios: list[float] = []
    for _ in range(_BOOTSTRAP_RESAMPLES):
        sample = rng.choices(case_scores, k=n)
        correct = sum(1 for s in sample if s.answer_score >= correct_answer_score)
        if correct:
            ratios.append(sum(s.cost_eur for s in sample) / correct)
    if len(ratios) < _BOOTSTRAP_RESAMPLES / 2:
        return None
    ratios.sort()
    low = ratios[int(_TAIL * len(ratios))]
    high = ratios[math.ceil((1 - _TAIL) * len(ratios)) - 1]
    return Interval(low=low, high=high)


def run_interval(
    run: EvalRun, metric: str, correct_answer_score: float | None = None
) -> Interval | None:
    """The interval for one of `run`'s headline metrics, or None when the run
    can't give one (too few cases, no cutoff, another metric)."""
    scores = run.case_scores
    if metric == "task_completion_rate":
        return wilson_interval(sum(s.task_completion for s in scores), len(scores))
    if metric == "answer_score_mean":
        return mean_interval([s.answer_score for s in scores], bounds=(0.0, 1.0))
    if metric == "cost_per_correct_answer_eur" and correct_answer_score is not None:
        return cost_per_correct_interval(scores, correct_answer_score)
    return None
