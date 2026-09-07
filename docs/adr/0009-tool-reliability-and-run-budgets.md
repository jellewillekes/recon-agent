# ADR 0009: Tool-layer retry/circuit-breaker, and per-run budgets

Status: Accepted
Date: 2026-09-07

## Context

Issue #13 (Step 8) asks for production semantics in the tool layer: retry with
exponential backoff and jitter, a circuit breaker returning `unavailable` after three
consecutive failures, and per-run budgets on tool calls, tokens, and wall-clock time with
graceful termination and a partial `AgentResult` on breach.

Three design questions this ADR settles:

**Where retry/breaker live.** `tools/server.py::_run_bounded` is already the one
chokepoint every tool funnels transient DuckDB failures through (`_ToolUnavailable`,
raised on a `duckdb.Error` or a `TIMEOUT_S` timeout). Wrapping it there means none of the
four public tool functions, or `_run_and_classify`/`_check_company`, need to change at
all — they already just catch `_ToolUnavailable` and build `status="unavailable"`.

**Circuit breaker scope and recovery.** Scoped **per DuckDB connection**, keyed by
`id(conn)` in a module-level dict — production holds one long-lived connection for the
server process's lifetime, and each test gets its own fresh
`duckdb.connect(":memory:")`, so this gives every test an independent breaker
automatically with no explicit reset needed. A first implementation opened the breaker
after three consecutive failed *calls* and simply never tried again — a real bug, caught
before shipping: an open breaker that never attempts a query again can also never observe
a success, so it can never close. Fixed with a cooldown: the breaker stays open for
`_BREAKER_COOLDOWN_S` after its most recent failure, then lets exactly one probe call
through. That call's outcome decides what happens next — success resets it fully closed,
another failure re-opens it and restarts the cooldown clock.

**Where budgets live.** Per-run budgets can't live in the tools layer — only a runtime
sees the full sequence of tool calls, tokens, and wall-clock time across a whole case;
one tool call doesn't. `RunBudget`/`_BudgetTracker`/`_BudgetExceeded` live in
`runtimes/agent_sdk.py`, shared by single mode (one `_run_query` call) and multi mode (up
to five, `runtimes/multi_agent.py`) since both already funnel through `_run_query`.
Tool-call and wall-clock budgets are checked **mid-stream**, inside `_run_query`'s
message loop — a breach there needs to actually stop an in-flight `query()` call, not
just refuse to start the next one. Token usage is only known once a call's
`ResultMessage` arrives (the SDK doesn't report it incrementally), so that budget is
checked **between calls** instead, in `run_multi_async`'s loop, before issuing the next
sub-call. In single mode there is no "next call" to skip, so a token-budget breach there
can only be reported after the one call finishes — a real limitation, not worked around.

## Decision

`tools/server.py`: `_run_bounded_once` (the original single-attempt logic, renamed) wraps
in a new `_run_bounded` that retries up to `_MAX_ATTEMPTS=3` total attempts with
exponential backoff (`_BASE_DELAY_S * 2**attempt`) plus jitter, behind a
`_CircuitBreaker` fetched via `_breaker_for(conn)`. An open breaker raises
`_ToolUnavailable` immediately, with wording distinct from a plain retry-exhausted
failure, so a trace can tell "the source is down" from "this one query failed."

`runtimes/agent_sdk.py`: `RunBudget` (`max_tool_calls`, `max_tokens`,
`max_wall_clock_s`), read from a new `run_budget:` section in `config/models.yaml`.
`_run_query` gained an optional `tracker: _BudgetTracker` parameter, checked after every
message; a breach explicitly closes the stream (`contextlib.aclosing`, not a bare
`break` — a bare break leaves the generator merely suspended, not closed; ADR-0006
established `query()`'s generator is safe to close mid-flight) and raises
`_BudgetExceeded` carrying whatever `tool_calls` were gathered before the breach.
`AgentSdkRuntime.run_async` catches it (before the generic `except Exception`, which
zeroes everything) and returns a real `AgentResult` — `confidence="low"`, `error` naming
which budget was breached, and the partial `tool_calls`/`tokens`/`cost` actually gathered,
not zeroed out. `runtimes/multi_agent.py::run_multi_async` wraps every `_run_query` call
in a local `_run` closure that re-raises a caught `_BudgetExceeded` enriched with
everything accumulated from *prior* completed sub-calls (`_run_query` itself only knows
about the call it's in), and checks the token budget itself after each call via the same
exception, so one conversion site in `run_async` handles both modes uniformly.

`config/models.yaml`'s `run_budget.max_wall_clock_s` (100s) is set a bit under
`api/main.py`'s own `RECON_API_REQUEST_TIMEOUT_S` default (120s) so a breach still returns
this budget's graceful partial `AgentResult` instead of the API's blunter 504 with no
body — for CLI/eval callers, who have no other wall-clock bound at all, this is their
only one.

## Consequences

- The circuit breaker's cooldown is a fixed constant (`_BREAKER_COOLDOWN_S = 30.0`), not
  itself exponential — a real production breaker often backs off the cooldown on repeated
  probe failures too. Not implemented here; the issue only asks for open-after-three, and
  a fixed cooldown is simpler and sufficient for the DuckDB fixture backend this runs
  against today.
- A token-budget breach in single mode is reported but not preventable — the one call has
  already finished by the time it's checked. Multi mode's between-call check is the piece
  that actually saves further spend.
- `RunBudget` numbers (30 tool calls, 300k tokens, 100s) are estimates with no production
  traffic to tune them against yet — generous enough not to constrain a normal run, tight
  enough to catch a genuine runaway. Revisit once real usage data exists.
- The breaker registry (`_BREAKERS`, keyed by `id(conn)`) grows one entry per distinct
  connection object for the life of the process; in production there's exactly one
  connection, so this never grows. Test suites create many short-lived connections, so the
  dict grows across a long test run, though each entry is small and this was judged
  acceptable rather than adding explicit teardown machinery for it.
