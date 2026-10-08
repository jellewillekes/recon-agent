# 0035: A verification report, a /verify endpoint and a claim check in the gate

## Context

ADR 0034 gives one claim a verdict. Three things were still open (#133). What an answer's
verdicts add up to. How another agent gets them. How the promotion gate learns that a new
version has more wrong claims even when its answer score held.

The gate compares stored run files and must work on them without a new run. Tool outputs
aren't stored on a result (docs/contracts.md §4), so verdicts have to be stored with the
run, not recomputed by the gate.

## Decision

- `VerificationReport` totals one answer's `ClaimVerification`s. `overall_verdict` is the
  worst claim: CONTRADICTED, then STALE, then UNSUPPORTED. With none of those it is
  UNVERIFIABLE when nothing could be read, PARTIALLY_SUPPORTED when some claims couldn't
  be, and SUPPORTED when all could. An answer with no claims is UNVERIFIABLE.
- `POST /verify` takes `claims` and `evidence` rows. A claim is checked against the rows it
  cites, or against all rows when it cites none. It doesn't extract claims from prose,
  because that needs a model. `question`, `answer` and `tool_calls` are left out until
  something uses them.
- `CaseScore.verifications` and `EvalRun.verifier_version` store the verdicts. Both are
  optional, so older runs load and count as "not measured".
- The gate compares only runs with the same `verifier_version`
  (`"<rules>:tol=<tolerance>"`; bump the rules when a verdict can change). A candidate that
  stopped verifying while the baseline did is refused. A baseline that never verified is
  skipped, not passed.
- The gate fails on a rise in the unsupported-and-contradicted share by more than
  `claim_bad_rate_noise_band`, and on any case that completed in the baseline and now has
  more contradicted claims. The band defaults to 0 and is the user's to set in
  `config/thresholds.yaml`.

## Consequences

- The rate is over claims the verifier could read. Counting a raw total would punish a
  longer answer, and counting UNVERIFIABLE would let an answer hide bad claims behind
  prose the verifier can't read.
- Agents write different claims on each run, so claims can't be matched between runs. The
  gate names cases and quotes the candidate's contradicted claims.
- The harness doesn't produce verifications yet. It needs the rows each claim cites, which
  means replaying the agent's tool calls (as ADR 0026 does for searches). Until that lands
  the claim check has no real data and the gate skips it. The follow-up is #139.
- With a band of 0, one extra unsupported claim fails the gate. That is strict for small
  case sets, where answers vary run to run (docs/eval-noise.md). Set the band from
  measured noise once runs exist.
