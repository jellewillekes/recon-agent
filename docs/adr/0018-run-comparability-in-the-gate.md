# ADR 0018: The promotion gate refuses runs that don't measure the same thing

Status: Accepted
Date: 2026-09-30

## Context

Issues #61 and #62. `check_gate` compared a candidate's metrics with a baseline's
without checking that the two runs measured the same thing. Four differences make the
numbers meaningless side by side:

- `rubric_version`. ADR 0014 moved it to "2", which changes how `answer_score` is
  computed.
- `dataset`.
- The tool data. Since #23 the agent answers from an EDGAR snapshot, and re-fetching or
  moving the cutoff changes what it can find. `EvalRun` didn't record which snapshot a
  run queried.
- The cases. A `--limit 3` candidate against a 50-case baseline compares averages over
  different questions.

Two choices were settled with the user. #62 adds a field to `EvalRun`, and
`docs/contracts.md` says a contract change is its own PR. It ships together with #61,
because both close the same hole. The case-set check wasn't in either issue, and was
added on the user's approval.

## Decision

- `EvalRun` gains an optional `tool_data_snapshot`: `"fixture"`, or the EDGAR snapshot's
  id. `tools.data_source.tool_data_snapshot_id()` computes it with the same selection
  logic as `open_tool_data()`. `recon.cli eval` passes it to the harness, so the harness
  depends on no tool module.
- `gate.comparability_failures` refuses a comparison when `rubric_version`, `dataset`,
  `tool_data_snapshot` or the set of case ids differ, or when the baseline has no
  `tool_data_snapshot`. `check_gate` runs it first and skips the metric rules when it
  fails, because a worse score on an incomparable run says nothing.
- `recon.cli eval --baseline` runs the same check before the eval starts, so no credit is
  spent on a run the gate is bound to refuse. It also resolves the snapshot before
  running, so a missing EDGAR cache fails up front, not in the first case.

## Consequences

- Effect on existing results: the one committed result still loads, with
  `tool_data_snapshot` empty. It can't serve as a baseline. No `evals/baseline.json`
  exists yet, so nothing that exists breaks.
- A baseline is tied to one snapshot. Re-fetching EDGAR means regenerating the baseline
  through an explicit PR, which costs a full run.
- Case order doesn't matter, only the set. A `--limit` run can only be gated against a
  baseline with the same limit.
- `runtime`, `mode`, prompts and model config may differ between the runs. Comparing
  those is the point of the gate.
