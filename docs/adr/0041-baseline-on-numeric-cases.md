# 0041: The baseline moves to the numeric cases

## Context

The prompts now ask the agent to state each figure it reads from fact rows as data (ADR
0040). A prompt change needs a new `evals/baseline.json` in the same PR. The old baseline
ran on 7 cases that need filing text. Rerun with the new prompts, those cases gave 0 of 17
checkable claims (`eval-20261008T190759Z`), so they can't measure the claim gate. Six
cases whose answers rest on fact rows (`evals/numeric-cases.txt`, #145) were run three
times. Which run would become the baseline was declared before the runs: the first.

## Decision

- `evals/baseline.json` is the first numeric run, `eval-20261010T205308Z`. It replaces the
  text-case baseline instead of sitting next to it, since the gate reads one baseline and
  only this one gives the claim check something to check.
- The text cases stay as the measurement for `search_knowledge` and retrieval.
- The three runs keep verifier version `3:tol=0`. The rule that a ratio stated as a multiple
  is UNVERIFIABLE landed under that version after the runs, before version 3 reached
  `main`. It changes one claim, in the third run, from CONTRADICTED to UNVERIFIABLE. Bumping
  the version would have made all three runs incomparable with later ones for that claim.
- `claim_bad_rate_noise_band` stays unset. The three runs had no real bad claim, so there
  is no spread to set it from.

## Consequences

- A candidate is now compared with runs over `evals/numeric-cases.txt` only (ADR 0018). A
  run over the text cases is no longer gated.
- With 6 cases, one case moves the answer score mean by about 0.17.
- The baseline checks 16 claims, short of the 20 #145 asks for. Two of the six cases are
  answered from filing text in every run and give no checkable claim.
- The stored third run still shows its pre-rule CONTRADICTED verdict. Results are never
  edited, so `docs/eval-noise.md` records it.
