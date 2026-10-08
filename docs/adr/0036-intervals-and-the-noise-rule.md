# 0036: Intervals report uncertainty; the gate's noise band stays the owner's

## Context

#135 asks for confidence intervals on a run's headline numbers, and for the gate's noise
band to come from measured variance. The baseline is 7 cases, so a bare 0.772 says too
little.

Two things get called noise, and they are different. A confidence interval says how far a
score could move on other cases. The gate compares two runs of the same cases, so the
noise it must allow for is run-to-run: how far a rerun moves. An interval is mostly the
spread between cases (0.15 in the baseline's per-case scores), which a rerun doesn't
change. Only repeat runs measure the second kind.

The gate's band of ±0.10 is the owner's value (ADR 0028, AGENTS.md). A band derived from
data would change what the gate decides without the owner choosing it.

## Decision

- Intervals are computed from stored case scores, so older runs work and no contract field
  is added. Task completion uses the Wilson score interval. The answer score mean uses a
  Student t interval, clipped to 0 to 1, because a bootstrap is too narrow at a handful of
  cases. Cost per correct answer uses a seeded bootstrap over cases, as a ratio has no
  simple formula.
- Run-to-run noise is measured from stored repeat runs: runs with the same dataset, rubric,
  tool data, model config, prompts, runtime, mode, routing and cases (`eval/noise.py`).
  The band is the t value times the standard deviation of the difference between two
  runs' answer score means, from each case's variance across the runs.
- The gate does not use either number. It keeps the fixed band, and its output says so
  (`gate.noise_rule`). `compare` and `GET /evals/compare` add the intervals, the rule, and
  a warning: noise unmeasured when no repeat of the baseline's settings is stored, or the
  measured band when it is wider than the gate's.

## Consequences

- The stored pair from #77 gives a band of about ±0.22, wider than ±0.10. The gate may
  fail an unchanged candidate. That rests on one pair, so it is a warning and not a change.
  Switching the gate to a measured band is the owner's decision, in its own PR.
- The warning appears only when `compare` is given the stored runs, as the CLI and API do.
- Judge failures leave placeholder zeros in `answer_score`, which would inflate the noise.
  The gate already refuses such runs (#123), but repeat groups don't exclude them.
- More cases narrow the intervals, and more repeat runs make the noise estimate firmer.
  Both need paid runs and are not done here.
