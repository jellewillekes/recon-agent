# 0038: The harness verifies claims by replaying fact calls

## Context

ADR 0035 stores claim verdicts on `CaseScore.verifications` and gates on them. Nothing
filled them in (#139). The verifier needs the rows each claim cites, and a stored
`AgentResult` keeps tool-call arguments but not their rows (docs/contracts.md §4). There
were two options. Store tool output on `ToolCall`, which is a contract change. Or replay
the calls, as ADR 0026 does for searches.

## Decision

- The harness replays the agent's `get_financial_fact` calls after each case, once per
  distinct set of arguments, and runs `verify_claims` on the claims and the replayed rows.
  No model is called.
- Replayed rows go through `with_refs` exactly as the MCP server's tool does. A ref is a
  hash of the tool, the company and the row (ADR 0030), so a replayed row carries the ref
  the agent cited.
- The replay is passed into `run_evaluation` as `facts`, like `passages`, so the harness
  depends on no tool module. `recon.cli eval` builds it from `open_tool_data()`, the data
  `tool_data_snapshot` names.
- With `facts`, every scored case gets `verifications`. An answer with no claims gets `[]`,
  meaning measured with nothing to check. `None` still means "not verified", which is what
  the gate's "not measured" rule reads. The run records `verifier_version()`.
- The markdown summary has a Claims section: counts per verdict, the bad-claim share the
  gate uses, and each contradicted claim. `GET /evals` gives the counts as
  `claim_verdicts`.

## Consequences

- No contract change on `ToolCall`. The replay is faithful only while the tool data is the
  data the run used, which `tool_data_snapshot` already pins.
- Only fact rows are replayed. A claim that cites only verified filing-text, filing or
  company rows is UNVERIFIABLE: it rests on real rows the numeric verifier can't read.
  Counting it as UNSUPPORTED would make the bad-claim share track how often the agent
  cites text, which is most of the baseline's qualitative cases. A claim that cites
  nothing, or a ref no tool returned, stays UNSUPPORTED.
- The committed baseline can't be verified after the fact. It keeps tool names, not the
  claims or the call arguments. It must be regenerated through its own PR. Its 7 cases
  cost about €1.42 a run, above the €1 default `--max-cost-eur`, so the cap is raised
  only with the user's go-ahead. Until then the gate skips the claim check (ADR 0035).
- `claim_bad_rate_noise_band` stays at its default of 0 until run-to-run noise in the
  bad-claim share is measured. The value is the user's to set.
