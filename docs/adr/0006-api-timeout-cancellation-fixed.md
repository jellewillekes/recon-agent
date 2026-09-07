# ADR 0006: Make a timed-out /investigate run genuinely cancellable

Status: Accepted
Date: 2026-09-07

## Context

`docs/adr/0005-api-timeout-cancellation-deferred.md` deferred this because the real fix
crosses the `Runtime` protocol boundary (`src/recon/runtimes/base.py`), which `CLAUDE.md`
gates behind explicit permission — not something an automated review-response loop should
decide, and bigger than PR #31's original scope. That permission was given directly:
tracked as issue #37, with instruction to fix it for real.

Tracing the actual mechanics confirmed the fix is straightforward once the protocol boundary
is crossed, not a deep redesign:

- `AgentSdkRuntime.run()` is synchronous only because it wraps an already-async
  implementation — `_run_async()` in the same module — in `asyncio.run()`. That inner
  function is a plain coroutine driving `claude_agent_sdk.query()`.
- `claude_agent_sdk`'s own internals (`_internal/client.py`, `_internal/query.py`) are
  explicitly built to be cancellation-safe: `process_query()`'s generator chain runs its
  cleanup in `finally` blocks, and `Query.close()` / `SubprocessCLITransport.close()` use a
  shielded cancel scope with every await bounded (~20s worst case) specifically so that
  cancelling the coroutine consuming `query()` still terminates the subprocess.
- The old code never got that benefit because `asyncio.to_thread(_runtime.run, case)` runs
  `run()` — including its own internal `asyncio.run()` — in a worker thread with a second,
  disconnected event loop. `wait_for`'s cancellation, fired in the API's main loop, can only
  cancel the await *on the thread*; it has no path into that second loop, so it never reaches
  `query()` at all.

## Decision

Add `run_async` to the `Runtime` protocol — a real coroutine version of `run`, same
contract, same error-to-`AgentResult` handling. `AgentSdkRuntime.run_async` awaits
`_run_async` directly instead of bridging through `asyncio.run()`; `run()` becomes a thin
sync wrapper (`asyncio.run(self.run_async(case))`) for the CLI and eval harness, which have
no event loop of their own to hand a coroutine to.

`api/main.py`'s `/investigate` now does
`asyncio.wait_for(_runtime.run_async(case), timeout=REQUEST_TIMEOUT_S)` — no thread bridge.
Cancellation now reaches `query()` in the same event loop, and through it, the SDK's own
shielded subprocess teardown.

Verified directly, not assumed: `tests/test_runtimes.py::test_run_async_cancellation_stops_the_underlying_query`
monkeypatches `query()` with a generator that hangs until cancelled, wraps `run_async` in
`asyncio.wait_for` with a short timeout, and asserts the generator's own cleanup actually ran.
`tests/test_api.py::test_investigate_timeout_is_504` asserts the same property through the
full HTTP path.

## Consequences

- A timed-out request's 504 can now be delayed by however long cleanup takes (bounded ~20s
  worst case by the SDK's own shield) instead of returning the instant the timeout fires.
  Deliberate: a slightly slower 504 with guaranteed cleanup is a better tradeoff than an
  instant 504 with an orphaned, unbounded background run — the gap ADR-0005 accepted.
- `_runtime.run(case)` no longer leaks past `MAX_CONCURRENCY` on a timeout; the semaphore and
  the actual run's lifetime are aligned again.
- Any future `Runtime` implementation (a second runtime/mode, step 7) must implement
  `run_async` too, not just `run` — enforced structurally by the Protocol and by
  `tests/test_eval_harness.py`'s `_FakeRuntime`, which now implements both.
- `docs/adr/0005-api-timeout-cancellation-deferred.md` stays as the historical record of why
  this was deferred first; it is not rewritten, only marked superseded.
