# ADR 0013: Judge any non-empty answer, keep task_completion strict

Status: Accepted
Date: 2026-09-28

## Context

Issue #53. `harness.py:score_case` treated any non-`None` `AgentResult.error` as a
failed run. It gave every rubric dimension a score of 0 and skipped the judge. But
runtimes also set `error` on runs that kept a complete answer:

- a token budget checked only after the run finished (`agent_sdk.py` single mode,
  `langgraph.py` single, multi and `resume`)
- a budget breach after SDK multi mode's synthesis or critic step already produced an
  answer (ADR-0009)
- a `langgraph×multi` run paused before its review-flag write (ADR-0010)

The paused case meant every flagged `langgraph×multi` case scored 0, whatever the answer
quality. A real failure always returns `answer=""`, from each runtime's catch-all handler
or from a breach before any answer exists.

Two options were considered. The first was to add an optional `AgentResult.warning`
field and move the non-fatal notes there. That is an allowed additive contract change,
but it touches three runtimes, `docs/contracts.md` §4 and ADR-0010's paused-run design.
The second was to change only the harness, grading based on whether an answer exists.

## Decision

Decided with the user. The harness judges any non-empty answer. It gives a hard 0
without calling the judge only when `answer.strip()` is empty. When a judged run also
carries an `error`, `CaseScore.notes` records both the error and
`answer judged despite runtime error`.

`metrics.task_completion` stays strict: `error is None` and a non-empty answer. A run
that breached its budget or paused didn't finish within its constraints, even when its
answer is worth grading.

## Consequences

- `answer_score` and `task_completion` now measure separate things on purpose: answer
  quality versus finishing cleanly within constraints. A case can score well on one and
  zero on the other.
- Flagged `langgraph×multi` cases still count as not completed, because the eval has no
  way to approve the pause. They lower `task_completion_rate` but no longer
  `answer_score_mean`.
- Judge calls now also run for these cases, which is a small extra judge cost per case.
- `AgentResult.error` keeps meaning "the run didn't end cleanly", not "the run failed".
  If an API consumer ever needs to tell these apart, the `warning` field is still the
  route to take, as a contract change in its own PR.
- The committed `eval-20260907T102452Z` was scored under the old rule and is not
  rescored. No baseline exists yet, so no gate comparison shifts.
