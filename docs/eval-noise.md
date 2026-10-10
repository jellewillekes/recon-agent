# Run-to-run noise

How much the eval's numbers move when nothing changes (#77). A delta smaller than
this is not a result.

## Measurement

The same 7 text cases (`evals/text-cases.txt`) were run twice on sdk/single, with
the same prompts, model config, rubric, dataset and tool data:
`eval-20261006T204756Z` (the baseline) and `eval-20261006T210553Z`. Measuring it
cost €2.56: €1.34 and €1.23.

| Metric | Run 1 | Run 2 | Difference |
|---|---|---|---|
| answer_score_mean | 0.652 | 0.696 | +0.044 |
| answer_correctness_mean | 0.619 | 0.563 | −0.056 |
| evidence_grounding_mean | 0.714 | 0.857 | +0.143 |
| tool_efficiency_mean | 0.643 | 0.786 | +0.143 |
| task_completion_rate | 1.000 | 0.857 | −0.143 |
| faithfulness_mean (4 cases) | 1.000 | 0.986 | −0.014 |
| retrieval metrics (all variants) | | | 0.000 |
| total_cost_eur | 1.336 | 1.228 | −0.109 |
| elapsed_ms_mean | 58,375 | 51,270 | −7,105 |

Per case, `answer_score` moved by up to 0.47. Paylocity's regulatory risks went from
0.31 to 0.78, and AMD's gross profit beat or miss went from 0.92 to 0.48. Two cases
scored the same both times. In run 2 the AMD case used 348,870 tokens, over the
300,000 budget, so it counted as not completed. Its answer was still judged (0.48), so
it stays in run 2's `answer_score_mean`. It took 121 s, inside the 150 s budget, so the
token budget is the limit it hit. In run 1 it completed.

Faithfulness was scored on 4 cases in each run, but not the same 4: Micron, Workday,
Smucker's and AMD in run 1, and Micron, Workday, Smucker's and Paylocity in run 2. Its
difference isn't like-for-like. Since `answer_score` renormalizes over the dimensions a
case was scored on (ADR 0014), the AMD and Paylocity swings also mix a change in which
dimensions counted with the judge's own variance.

## What it means

- **The means hide large per-case swings.** Two cases moving about 0.45 in opposite
  directions left `answer_score_mean` within 0.05. A single case moving 0.47 shifts a
  7-case mean by about 0.07 on its own.
- **Treat a change in `answer_score_mean` under about 0.1 on these 7 cases as no
  change, as a working rule.** The observed difference was 0.044, and one case can
  move the mean by about 0.07. Two runs give one sample of the difference, not a band,
  so 0.1 is a judgement until more runs measure one.
- **Rubric means and task completion move in steps of 0.14**, one case each. A step
  of one case is within noise.
- **Retrieval metrics don't move.** They search with the question, not the agent's
  queries, and need no model. Any change in them is real, though with 4 labelled
  cases it rests on little data.
- **Cost varied by about 8%.**

## Intervals, and what they are not

A 95% interval on a run's score (ADR 0036) answers a different question from the noise band.

- **The interval** says how far the score could move on other cases than the ones run. The
  baseline's answer score of 0.772 on 7 cases has a Student t interval of 0.63 to 0.91.
  Task completion of 7 of 7 has a Wilson interval of 0.65 to 1.00. Cost per correct answer
  of €0.203 has a bootstrap interval of €0.11 to €0.34. `recon.cli eval` writes them into the
  run's markdown summary, and `recon.cli compare` and `GET /evals/compare` show them.
- **The noise band** is how far a rerun of the same cases moves. Only repeat runs measure it.
  The interval doesn't, because it is mostly the spread between cases (the baseline's
  per-case scores have a standard deviation of 0.15), and a rerun sees the same cases.

The two runs above are stored repeats: same dataset, rubric, tool data, model config,
prompts, mode and cases. `recon.eval.noise` measures them. Their per-case differences are
0.0, 0.0, 0.1, 0.07, 0.1, 0.47 and −0.43, which gives a standard deviation of 0.094 for the
difference between two runs' `answer_score_mean`, and a 95% band of **about ±0.22**.

That is wider than the gate's fixed ±0.10 (ADR 0028). If it holds, the risk is the gate
failing an unchanged candidate, not missing a real drop. One pair is a thin sample, and two
cases that moved by 0.47 and −0.43 carry most of it. The gate still uses ±0.10, which is the
owner's value. `compare` prints the measured band as a warning next to it.

Reaching a ±0.10 band by averaging alone would take roughly 5 times as many cases as the
7 (the band shrinks with the square root of the cases), or more repeat runs to measure it
properly. Neither is done here.

## A second repeat, on rubric 4 (2026-10-08)

`eval-20261008T162733Z` reran the current baseline's 7 text cases on sdk/single for #139,
with the same dataset, rubric, tool data, model config and cases. Nothing the run reads
had changed since the baseline. Only `prompts/supervisor_langgraph.md` differs, which
sdk/single doesn't read. That difference still keeps `recon.eval.noise.repeat_groups` from
pairing the two runs on its own. It cost €1.42: €0.87 agent and €0.56 judges.

| Metric | Baseline `eval-20261007T175852Z` | Rerun | Difference |
|---|---|---|---|
| answer_score_mean | 0.772 | 0.645 | −0.127 |
| answer_correctness_mean | 0.659 | 0.548 | −0.111 |
| tool_efficiency_mean | 0.929 | 0.571 | −0.357 |
| evidence_grounding_mean | 0.857 | 0.857 | 0.000 |
| task_completion_rate | 1.000 | 1.000 | 0.000 |
| retrieval metrics (all variants) | | | 0.000 |
| total_cost_eur | 1.423 | 1.425 | +0.002 |

One case carries most of the drop. `731403050270` fell from 0.94 to 0.31 and was classed
as a reasoning failure. Every other case moved by 0.15 or less. The gate fails the rerun
against the baseline, since 0.127 is beyond its ±0.10 band. That is the risk the section
above predicted: the gate failing an unchanged candidate.

`estimate_noise` on the pair gives a standard deviation of 0.098 for the difference of two
means and a 95% band of about ±0.23. The first pair gave 0.094 and ±0.22. Two pairs now
agree, under two budgets and two rubrics. The gate's band is still the owner's value.

The rerun was not made the baseline. It scored 0.127 lower for no reason but noise, so
adopting it would have lowered the bar the gate holds candidates to.

### Claim verdicts on these cases

The rerun is the first with claims verified (ADR 0038). Of its 32 claims, 1 was SUPPORTED
and 31 UNVERIFIABLE. Nineteen cited only filing text, 6 named no concept the rows held, 4
were negated and 2 gave a cause or an outlook. The agent made 26 `search_knowledge` calls
and 5 `get_financial_fact` calls. These cases were picked to need filing text, so the
numeric verifier has almost nothing to check on them. Its bad-claim share rests on one
claim, so `claim_bad_rate_noise_band` can't be measured here. That needs cases with
figure questions.

## Not done here

- The gate now treats a drop of up to 0.10 in the answer score, or one case in task
  completion, as noise (ADR 0028). Both values are the user's. The two runs above used
  the earlier 300k-token budget, so the band isn't remeasured for the
  current baseline's config.
- Two runs give one difference per metric. A real band needs more runs, which cost
  about €1.30 each on these cases.

## The numeric cases, three runs (2026-10-10)

The 6 cases in `evals/numeric-cases.txt` (#145) were run three times on sdk/single with the
figure prompts (ADR 0040). The first run was declared the baseline before any ran (ADR 0041).

| Run | Answer score | Claims | Checked | Bad | Bad share (95% Wilson) | Cost (€) |
|---|---|---|---|---|---|---|
| `eval-20261010T205308Z` (baseline) | 0.801 | 31 | 16 | 0 | 0.00 (0.00 to 0.19) | 0.69 |
| `eval-20261010T210005Z` | 0.797 | 33 | 14 | 0 | 0.00 (0.00 to 0.22) | 0.60 |
| `eval-20261010T210644Z` | 0.789 | 38 | 21 | 1 | 0.05 (0.01 to 0.23) | 0.70 |

The answer score moved by 0.012 across the three runs, far less than on the text cases.
Per case it moved by up to 0.30, on two cases.

The one bad claim is not a real error. The agent gave an inventory turnover of 6.49 times
as a ratio figure. The verifier read a ratio as a percentage and compared 6.49 with 648.5.
The rule added after the runs makes such a claim UNVERIFIABLE (ADR 0040), so under it all
three runs have 0 bad claims. The stored result still shows the earlier verdict.

Checked claims, out of the claims made, per case and run:

| Case | Run 1 | Run 2 | Run 3 |
|---|---|---|---|
| `ed95ff9ff29c` | 2 of 3 | 0 of 3 | 2 of 3 |
| `1e6fcc9bda5d` | 9 of 13 | 9 of 13 | 13 of 16 |
| `9e7e8a3f7139` | 3 of 5 | 3 of 4 | 4 of 5 |
| `135cf3369c99` | 0 of 3 | 0 of 6 | 0 of 6 |
| `fd98327e142d` | 2 of 3 | 2 of 3 | 2 of 3 |
| `caf4be73b29f` | 0 of 4 | 0 of 4 | 0 of 5 |

Of the 51 unchecked claims, 28 cite only filing text and 10 put a second number in the
claim's text. Seven give a ratio figure with the wrong rows or scale, and 3 have a text
that doesn't state the figure. Two give a cause or an outlook, and 1 states a change in a
percentage concept. Two cases never yield a
checked claim: the agent answers them from filing text although the fact rows hold the
figures.

With no real bad claim in three runs, `claim_bad_rate_noise_band` can't be measured from
the spread. It stays unset.
