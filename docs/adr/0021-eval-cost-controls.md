# ADR 0021: Eval cost controls

Status: Accepted
Date: 2026-10-01

## Context

Every eval case spends Agent SDK credit from a personal subscription, and the user asked
to keep that as low as possible. Before this change, the only ways to bound a run were
`--limit N`, which takes the first N cases in dataset order, and remembering not to
start a full run.

The cost estimate was stale. The one committed result (3 cases, €1.12) predates the
isolation fix in ADR 0016. #67 measured the same three cases afterwards at €0.128,
about €0.04 per case. That's roughly €2–3 for all 50 cases and €0.30 for seven. The
committed result doesn't split agent from judge cost, so neither share was known.

## Decision

- **Subsets.** `eval --cases FILE` runs a fixed list of case ids, and `eval --company
  TICKER` runs the questions about one company. `evals/smoke-cases.txt` holds 7 cases
  about 7 different companies, picked by `recon.cli cases --spread 7`. The command walks
  case ids in order and takes single-company questions, so the choice is blind to
  scores. The user preferred 7 companies over 7 questions about one company, at the
  same cost. The gate already compares only runs over the same case set (ADR 0018).
- **A cap per run.** `--max-cost-eur`, €1 by default. The CLI refuses to start when the
  estimate (cases × €0.05) exceeds it. During a run, the harness stops before a case
  that could pass the cap, judging by the dearest case so far. A capped run is written
  and marked with `cases_skipped_at_cost_cap`, and the CLI exits non-zero. The gate
  refuses a capped run as a candidate and as a baseline, since it didn't measure its
  whole case set.
- **Cost split.** Every run's aggregate records `agent_cost_eur` and `judge_cost_eur`.
- **Judge on Haiku 4.5.** The judge marks a fixed list of statements true or false
  against an answer it's given, with no tools. The user chose Haiku 4.5 over Sonnet 5
  to halve the judge's share. A different grader scores the same answer differently,
  so `RUBRIC_VERSION` goes from `"2"` to `"3"` and the gate refuses comparisons across
  the change. No baseline existed yet, so nothing comparable is lost.
- The guard hook accepts `--cases` and `--company` as bounded runs, like `--limit`.

## Consequences

- The cheapest meaningful run is the smoke set at about €0.35. A baseline over it is
  compared only with runs over the same file. With 7 cases, one case moves a mean by
  0.14, so only large differences mean anything.
- The €0.05 estimate is a constant in `recon/cli.py`. Replace it with the measured
  per-case cost once a run has recorded the split.
- The first case always runs, because nothing is known about its cost yet. A single
  case is bounded by `run_budget` in `config/models.yaml`.
- Whether Haiku grades reliably enough is untested. If its scores look wrong on the
  first run, moving back is a config change and another `RUBRIC_VERSION` bump.
