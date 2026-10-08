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

## Not done here

- The gate now treats a drop of up to 0.10 in the answer score, or one case in task
  completion, as noise (ADR 0028). Both values are the user's. The two runs above used
  the earlier 300k-token budget, so the band isn't remeasured for the
  current baseline's config.
- Two runs give one difference per metric. A real band needs more runs, which cost
  about €1.30 each on these cases.
