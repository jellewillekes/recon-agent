# ADR 0026: Faithfulness by replaying searches

Status: Accepted
Date: 2026-10-06

## Context

Issue #18 asks for a faithfulness metric: are the answer's claims backed by the passages
the agent retrieved? ADR 0025 left it open with two options. A judge assertion can't see
the passages, because the judge only gets each tool call's name, arguments and status. Storing
tool outputs on `ToolCall` would show them, but it's a contract change, which
`docs/contracts.md` says goes in its own PR.

## Decision

The user decided: replay the agent's searches.

- `search_knowledge` is deterministic for a fixed index and fixed models. Each call's
  `query` and `top_k` are already on `ToolCall.arguments`. The harness runs those
  queries again and gets back the passages the agent saw.
- A separate judge call, on the judge model, lists the answer's claims from filing text
  and marks each supported or not. The prompt is `prompts/judge_faithfulness.md`. A
  case's score is the share of supported claims.
- The score goes into `CaseScore.rubric_scores` as `faithfulness`. It isn't a rubric
  dimension, so `answer_score` ignores it and the rubric version stays the same. The run
  reports `faithfulness_mean`.
- A case that didn't search, or made no claim from filing text, gets no score instead of
  a vacuous 1.0. The mean covers scored cases only.
- The extra judge call is added to the case's cost.

## Consequences

- No contract change. Runs before this one remain comparable.
- The replay is only faithful while the index, the corpus and the models are the ones
  the run used. Both are fixed by the corpus id in `tool_data_snapshot`, and the replay
  happens straight after each case.
- Only `search_knowledge` is covered. The rubric `evidence_grounding` still can't see what
  the fact tools returned. Covering those needs tool outputs on `ToolCall`, a separate
  contract change.
- Small samples: faithfulness is scored only on cases that searched and made claims from
  text, so a mean may rest on very few cases. Report the count next to it.
