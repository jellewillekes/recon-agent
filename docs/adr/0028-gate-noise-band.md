# 0028: The gate allows for measured run-to-run noise

## Context

The promotion gate failed a candidate when task completion dropped at all, or when the
answer score dropped by more than 2% of the baseline's. Two identical runs of the 7 text
cases (docs/eval-noise.md, #77) differed by one case in completion and by 0.044 in the
answer score. Single cases moved by up to 0.47. The old gate rejected that unchanged
repeat in both directions, so every comparison would have failed on noise alone.

## Decision

Decided by the user on 2026-10-07:

- The answer score may drop by up to 0.10, absolute, before the gate fails.
- Task completion may drop by up to one case.
- The cost rule is unchanged: more than 20% higher fails unless completion rose. Cost
  moved by 8% between the identical runs.
- The gate reports "same", "better" or "worse" per metric, and a change inside the band
  is "same".

## Consequences

- A real regression smaller than the band passes. On 7 cases that's the price of not
  failing on noise. More cases, or more repeat runs to measure a proper band, would
  narrow it.
- The band is a judgement from two runs, not a measured interval. Revisit it when more
  repeat runs exist.
