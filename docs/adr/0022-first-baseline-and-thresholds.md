# ADR 0022: The first baseline and its thresholds

Status: Accepted
Date: 2026-10-01

## Context

The gate and step 11's CI check (#16) need a committed baseline and the values in
`config/thresholds.yaml`. The user asked to keep credit spending as low as possible
(ADR 0021), so the baseline runs on the 7-case smoke set rather than all 50 cases. The
first smoke run, `eval-20261001T085035Z` (sdk, single mode, judge on Haiku 4.5), scored
`answer_score_mean` 0.566 with a task completion of 7 of 7, and cost €0.68.

## Decision

The user decided, after seeing the scores:

- That run becomes `evals/baseline.json`.
- `correct_answer_score` is 0.5. Five of the seven cases reach it, giving €0.14 per
  correct answer.
- `baseline_minimums` are `answer_score_mean` 0.5 and `task_completion_rate` 0.85, a
  little below the run's numbers, leaving room for one case of noise.
- No second run to measure noise yet (#77). It would have cost another €0.68.

## Consequences

- Future smoke runs on sdk single are compared against this baseline. Runs over other
  cases, or with a different rubric version, are refused (ADR 0018).
- Result files store scores, not answers, so the Haiku judge's grading can't be checked
  from them. Checking it means rerunning a case, or storing answers, which would be a
  contract change.
- Seven cases make a noisy reference. One case moves a mean by 0.14, so only large
  differences against this baseline mean anything until the noise is measured.
