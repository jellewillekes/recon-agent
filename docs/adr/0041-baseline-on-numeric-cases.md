# 0041: The baseline moves to the numeric cases

## Context

The prompts now ask the agent to state each figure it reads from fact rows as data (ADR
0040). A prompt change needs a new `evals/baseline.json` in the same PR. The old baseline
ran on 7 cases that need filing text. Rerun with the new prompts, those cases gave 0 of 17
checkable claims under verifier `2:tol=0` (`eval-20261008T190759Z`), so they can't measure
the claim gate. That run also scored 0.496 against the baseline's 0.772, with task
completion at 0.857: `408f6ef07a58` ran past its 150 s budget and `56d6f9e6e1a4` fell from
0.92 to 0.33. The drop of 0.276 is past the gate's 0.10 band and the ±0.23 rerun band
(#144). With 7 cases it can't be told apart from the prompt change or from noise. Six
cases whose answers rest on fact rows (`evals/numeric-cases.txt`, #145) were run three
times. Which run would become the baseline was declared before the runs: the first.

## Decision

- `evals/baseline.json` is the first numeric run, `eval-20261010T205308Z`. It replaces the
  text-case baseline instead of sitting next to it, since the gate reads one baseline and
  only this one gives the claim check something to check.
- The owner accepted the text-case drop above as the price of the swap, after seeing it.
  It is not gated, since the baseline moves in the same PR.
- The text cases stay as the measurement for `search_knowledge` and retrieval, now
  ungated. A text-case rerun with these prompts would show whether the drop holds.
- The three runs keep verifier version `3:tol=0`. The rule that a ratio stated as a multiple
  is UNVERIFIABLE landed under that version after the runs, before version 3 reached
  `main`. It changes one claim, in the third run, from CONTRADICTED to UNVERIFIABLE. Bumping
  the version would have made all three runs incomparable with later ones for that claim.
- Two more changes landed under version 3 after the runs. A `pure` concept is converted per
  concept, not per row, and a change in a converted rate is UNVERIFIABLE. Neither changes
  a run's verdict: every claim on a `pure` row cites one row within ±1, and the one claim
  about the change in a rate was already UNVERIFIABLE.
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
