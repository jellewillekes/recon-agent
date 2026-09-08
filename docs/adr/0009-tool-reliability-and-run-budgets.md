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

**Circuit breaker scope and recovery.** Scoped **per DuckDB connection**, keyed by the
connection object itself in a module-level `weakref.WeakKeyDictionary`. Today, every real
caller (`runtimes/agent_sdk.py`, `runtimes/multi_agent.py`, `api/health.py`) spawns a fresh
`-m recon.tools.mcp_server` subprocess — and so a fresh `duckdb.connect(":memory:")` — per
`query()` call, the same short connection lifetime a test's `conn` fixture has. The breaker
still does real work within that lifetime: it protects the run of tool calls inside *one*
`query()` call (single mode's one call; each of multi mode's up to seven role calls), just
not across separate calls or cases the way a persistent server process would let it. Keying
by the connection object rather than a single global still gives every test — and every
subprocess — an independent breaker automatically with no explicit reset needed, and no
stale entry once a connection is garbage-collected (a plain `id(conn)`-keyed dict would
risk a later connection reusing a freed one's address and inheriting its breaker state). A
first implementation opened the breaker after three consecutive failed *calls* and simply
never tried again — a real bug, caught before shipping: an open breaker that never
attempts a query again can also never observe a success, so it can never close. Fixed
with a cooldown: the breaker stays open for
`_BREAKER_COOLDOWN_S` after its most recent failure, then lets exactly one probe call
through. That call's outcome decides what happens next — success resets it fully closed,
another failure re-opens it and restarts the cooldown clock.

**Where budgets live.** Per-run budgets can't live in the tools layer — only a runtime
sees the full sequence of tool calls, tokens, and wall-clock time across a whole case;
one tool call doesn't. `RunBudget`/`_BudgetTracker`/`_BudgetExceeded` live in
`runtimes/agent_sdk.py`, shared by single mode (one `_run_query` call) and multi mode (up
to seven — decompose, up to four workers, synthesis, critic —
`runtimes/multi_agent.py`) since both already funnel through `_run_query`.
Tool-call and wall-clock budgets are checked **mid-stream**, inside `_run_query`'s
message loop — a breach there needs to actually stop an in-flight `query()` call, not
just refuse to start the next one. Token usage is only known once a call's
`ResultMessage` arrives (the SDK doesn't report it incrementally), so that budget is
checked **between calls** instead, in `run_multi_async`'s loop, before issuing the next
sub-call. In single mode there is no "next call" to skip, so a token-budget breach there
can only be reported after the one call finishes — a real limitation, not worked around.

**Correction, round 4 of PR #44's review:** this ADR claimed the single-mode limitation
above before the code actually implemented it — `run_async`'s single-mode branch never
checked `budget.max_tokens` at all, so a breach there was neither prevented nor reported,
contradicting "reported after the fact" above. Fixed: after the one `_run_query` call
completes, `run_async` checks `outcome.tokens_in + outcome.tokens_out` against
`budget.max_tokens` and, on a breach, sets `AgentResult.error` to say so — keeping the
real answer/evidence/confidence rather than discarding them, the same principle as the
round-2 fix for multi mode (a call that already produced a complete, valid result must not
have that result thrown away just because reporting the breach happens after the fact).

**Retry-blocking question, rounds 2-3.** Whether `_run_bounded`'s `time.sleep` and
`future.result(timeout=TIMEOUT_S)` block the MCP subprocess's event loop, stalling every
other in-flight tool call in it, not just the failing one. Resolved by inspecting
`mcp.server.mcpserver`'s own `FuncMetadata.call_fn`, whose docstring states directly: "a
sync function runs on a worker thread" (dispatched via `anyio.to_thread.run_sync`). Every
tool in `tools/mcp_server.py` is a plain `def`, not `async def`, so this applies to all of
them — the blocking happens in a worker thread, not the event loop that keeps the stdio
transport (and dispatch of any other concurrent tool call) responsive. No code change
needed; this was a real question worth confirming rather than assuming, not a bug.

## Decision

`tools/server.py`: `_run_bounded_once` (the original single-attempt logic, renamed) wraps
in a new `_run_bounded` that retries up to `_MAX_ATTEMPTS=3` total attempts with
exponential backoff (`_BASE_DELAY_S * 2**attempt`) plus jitter, behind a
`_CircuitBreaker` fetched via `_breaker_for(conn)`. An open breaker raises
`_ToolUnavailable` immediately, with wording distinct from a plain retry-exhausted
failure, so a trace can tell "the source is down" from "this one query failed." A
`TIMEOUT_S` timeout raises the narrower `_ToolTimeout` instead, which `_run_bounded` never
retries — it already waited the full `TIMEOUT_S`, so retrying would multiply that wait
rather than shorten the time to a breaker decision. Every other `_ToolUnavailable` still
retries normally, since those failures are typically fast (e.g. a `duckdb.Error`) and
retrying them costs on the order of `_BASE_DELAY_S`, not `TIMEOUT_S`.

`runtimes/agent_sdk.py`: `RunBudget` (`max_tool_calls`, `max_tokens`,
`max_wall_clock_s`), read from a new `run_budget:` section in `config/models.yaml`.
`_run_query` gained an optional `tracker: _BudgetTracker` parameter, checked after every
message; a breach explicitly closes the stream (`contextlib.aclosing`, not a bare
`break` — a bare break leaves the generator merely suspended, not closed; ADR-0006
established `query()`'s generator is safe to close mid-flight) and raises
`_BudgetExceeded` carrying whatever `tool_calls` were gathered before the breach. The
check only runs while `result_message` is still unset: once a message has produced a
complete, valid answer there is nothing left to protect by raising, and doing so anyway
would discard that answer in favor of an empty one. `AgentSdkRuntime.run_async` catches
`_BudgetExceeded` (before the generic `except Exception`, which zeroes everything) and
returns a real `AgentResult` — `error` naming which budget was breached, and the partial
`tool_calls`/`tokens`/`cost`/`answer`/`evidence`/`confidence` actually gathered, not zeroed
out. `runtimes/multi_agent.py::run_multi_async` wraps every `_run_query` call in a local
`_run` closure that re-raises a caught `_BudgetExceeded` enriched with everything
accumulated from *prior* completed sub-calls (`_run_query` itself only knows about the
call it's in), and checks the token budget itself after each call via the same exception,
so one conversion site in `run_async` handles both modes uniformly. Its supervisor-
synthesis and critic calls pass their own already-validated `answer`/`evidence`/
`confidence` into that check, for the same reason `_run_query`'s check stops once it has
one: a token breach on the call that *produces* the final answer must not throw that
answer away.

`config/models.yaml`'s `run_budget.max_wall_clock_s` (100s) is set a bit under
`api/main.py`'s own `RECON_API_REQUEST_TIMEOUT_S` default (120s) so a breach still returns
this budget's graceful partial `AgentResult` instead of the API's blunter 504 with no
body — for CLI/eval callers, who have no other wall-clock bound at all, this is their
only one.

## Consequences

- The breaker opens after three consecutive *calls* fail, not three raw failures — each
  call already retries internally, so up to `_MAX_ATTEMPTS=3` real failures can happen
  per call before it counts as one. Worst case is `_BREAKER_THRESHOLD * _MAX_ATTEMPTS = 9`
  failures before the breaker opens. This is intentional: retry absorbs a transient blip
  within one call, the breaker reacts to a call that keeps failing even after retrying.
  With `_ToolTimeout` never retried (above), the expensive failure mode (a stuck source)
  no longer compounds with `_MAX_ATTEMPTS`, so the 9x multiplier only applies to the fast
  failure modes where it costs on the order of `_BASE_DELAY_S`, not `TIMEOUT_S`.
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
