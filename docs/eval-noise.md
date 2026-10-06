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
| total_cost_eur | 1.336 | 1.228 | −0.108 |
| elapsed_ms_mean | 58,375 | 51,270 | −7,105 |

Per case, `answer_score` moved by up to 0.47. Paylocity's regulatory risks went from
0.31 to 0.78, and AMD's gross profit beat or miss went from 0.92 to 0.48. Two cases
scored the same both times. In run 2 the AMD case used 348,870 tokens, over the
300,000 budget, so it counted as not completed. In run 1 it completed.

## What it means

- **The means hide large per-case swings.** Two cases moving about 0.45 in opposite
  directions left `answer_score_mean` within 0.05. A single case moving 0.47 shifts a
  7-case mean by about 0.07 on its own.
- **Treat a change in `answer_score_mean` under about 0.1 on these 7 cases as no
  change.** That's two runs, so it's a floor on the noise, not a measured band.
- **Rubric means and task completion move in steps of 0.14**, one case each. A step
  of one case is within noise.
- **Retrieval metrics don't move.** They search with the question, not the agent's
  queries, and need no model. Any change in them is real, though with 4 labelled
  cases it rests on little data.
- **Cost varies by about 10%.**

## Not done here

- The gate still compares means without a noise band. #77 says the band is a
  threshold change, so it's the user's decision, in its own PR.
- Two runs give one difference per metric. A real band needs more runs, which cost
  about €1.30 each on these cases.
