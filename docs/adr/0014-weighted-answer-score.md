# ADR 0014: answer_score is the weighted mean across rubric dimensions

Status: Accepted
Date: 2026-09-28

## Context

Issue #51. `docs/contracts.md` §9 gates on a "weighted `answer_score`", and each
`config/rubrics/*.yaml` sets a `weight` (0.5 correctness, 0.3 grounding, 0.2
efficiency). Nothing read `Rubric.weight`. `answer_score` was the `answer_correctness`
dimension alone, so changing a weight had no effect on any score or gate decision.

Three options were considered:

- Compute a real weighted score and make it `answer_score`.
- Drop `weight` and reword §8/§9 to gate on correctness only.
- Keep `answer_score` as correctness and add a separate weighted metric.

## Decision

Decided with the user: `answer_score` becomes the weighted mean of the per-dimension
scores (`metrics.weighted_answer_score`). This matches §9's original intent without a
contract field change, and `gate.py` keeps comparing `answer_score_mean`.

Only dimensions the judge actually scored count, and their weights are renormalized.
The judge omits a dimension when a case yields no assertions for it, which is not the
same as scoring it zero. A run with no answer still scores 0.0 on every dimension
(ADR-0013). `load_rubrics` rejects a rubric set whose weights sum to 0.

`RUBRIC_VERSION` goes from `"1"` to `"2"`, per §8's rule that a scoring change breaks
comparability with earlier runs.

## Consequences

- The headline score moves a lot. The committed `eval-20260907T102452Z` would go from
  0.181 to 0.557 under these weights, because grounding scores 1.0 against the
  synthetic fixture while correctness stays low. `answer_correctness_mean` is still in
  every aggregate, so the correctness-only number stays visible.
- The promotion gate now depends on the `evidence_grounding` and `tool_efficiency`
  assertions, which are still marked draft. Finalizing them, and the weights
  themselves, is the user's call (CLAUDE.md "do not delegate").
- Runs with different `rubric_version` values aren't comparable, but `gate.py` doesn't
  check this yet. That's worth adding before the first baseline is recorded.
- The committed rubric-version-1 result is not rescored.
