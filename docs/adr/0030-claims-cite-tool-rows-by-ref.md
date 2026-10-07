# 0030: Claims cite tool rows by ref, and the server checks them

## Context

`AgentResult.evidence` was a list of strings the model wrote. `search_knowledge` returns
the company, form, filing date, accession and section of every passage, and
`get_financial_fact` returns the filing each value came from, but none of that survived
into the answer. Nothing linked a statement in the answer to the passage behind it, and
nothing checked that a cited passage had really come back from a tool. The workspace
(#93) needs citations it can show with their source, and the evaluation needs to know
which claims are supported (#115).

Options considered for how the model points at its sources:

- Free-text evidence with a quote, matched against tool output afterwards. Matching
  paraphrased numbers and passages is fuzzy, and the source details would still have to
  be guessed.
- "Call 3, row 2". Positions change between workers, retries and replays, and multi
  mode's workers each number their own calls.
- A ref per row, derived from the row's content. Chosen.

## Decision

Decided by the user on 2026-10-07 (#115):

- Every row the five read tools return carries `ref`: `"E"` plus the first 12 hex
  characters of the sha256 of the tool name, the company the call asked about and the
  row's canonical JSON. The company is in it because a concept row doesn't carry one.
  It's added in one place, the MCP server's tool wrappers, so both runtimes get it.
- The answer schema asks for claims, each with its importance (`key` or `supporting`)
  and the refs it rests on. The model no longer writes evidence strings.
- The runtime keeps the rows its tools returned during the run and resolves every cited
  ref against them. A ref that matches is `verified`; its source details and excerpt are
  rendered from the row. A ref that matches nothing is kept, unverified, so a made-up
  citation shows instead of disappearing. Nothing the model writes ends up in an excerpt.
- `AgentResult.claims` and `evidence_items` are new optional fields. `evidence` stays,
  filled with one line per resolved source, so the judges, the faithfulness check and
  older readers keep working. If the model returns no claims, the run still succeeds,
  with empty claims.
- The `evidence_grounding` judge rubric is unchanged. Two deterministic metrics sit next
  to it: `claim_support_rate` (key claims with at least one verified ref) and
  `citation_precision` (cited refs that are verified).
- `RUBRIC_VERSION` goes to 4, because the judges now see differently shaped evidence.
- Saved research runs (the API's run store) go in Postgres, in a `research_runs` table.
  It's transactional state, so it belongs there under the storage boundary, and the
  same Postgres already serves `search_knowledge`.

## Consequences

- A citation is checked by lookup, not by fuzzy matching, and the UI can show exact
  source details for every verified one.
- "Verified" means the row was returned in this run, not that it supports the claim.
  Whether it does is still the judges' and the critic's call.
- Tool output grows by about 16 characters per row.
- Version 3 and version 4 runs can't be compared by the gate. New baselines are recorded
  with this change.
- Rows are held in memory for the length of a run only; they aren't stored in
  `AgentResult` or in eval results.
