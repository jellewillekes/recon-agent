# ADR 0005: Defer real cancellation of a timed-out /investigate run

Status: Accepted
Date: 2026-09-07

## Context

`POST /investigate` (PR #31) wraps `AgentSdkRuntime.run()` in
`asyncio.wait_for(asyncio.to_thread(_runtime.run, case), timeout=REQUEST_TIMEOUT_S)`.
`AgentSdkRuntime.run()` is a synchronous method that internally calls `asyncio.run()` and
spawns an MCP stdio subprocess (`src/recon/runtimes/agent_sdk.py`). Every one of the
automated review's four rounds on PR #31 flagged the same gap: `wait_for`'s timeout cancels
the *await*, not the worker thread or the subprocess underneath it. The client gets a 504,
`_semaphore` releases, but `_runtime.run(case)` keeps executing in the background — outside
`MAX_CONCURRENCY`, still spending Claude API credit, until it finishes or crashes on its own.

A real fix means making the run actually cancellable, which the current `Runtime` protocol
(`src/recon/runtimes/base.py`) doesn't support — it's a plain sync `run(case) -> AgentResult`,
used synchronously by the CLI and eval harness as well as the API. Two ways to fix it, both
considered:

- Add an async-native path (e.g. `run_async`) so the API can await the real coroutine
  directly instead of bridging through a thread, letting cancellation propagate into the
  `query()` loop and (assuming the SDK cleans up on cancellation) the subprocess with it.
- Pass a cancellation token/event into `AgentSdkRuntime.run()` so a timeout can signal the
  subprocess to stop without a full async API.

Both extend `Runtime`, which `CLAUDE.md`'s "Forbidden without explicit permission" list
covers under "changing module boundaries" — not something to decide inside an automated
review-response loop, and bigger than PR #31's own stated scope (exposing the existing
runtime as a service, not redesigning the runtime interface).

## Decision

Defer the real fix. Merge PR #31 with the gap open, but no longer silent: on a timeout,
`main.py` now logs a warning naming the request and case ID, so an orphaned run is at least
observable instead of vanishing without a trace. Track the actual fix as a follow-up issue
rather than letting it live only as a recurring review comment.

## Consequences

- `MAX_CONCURRENCY` is not a hard bound today — a slow or hung run can exceed it after a
  timeout, until the orphaned thread finishes on its own.
- Anyone running this service under real load should watch for the new warning log line
  (`request_id=... timed out after ...`) as the signal that this gap is live in production,
  not just in review comments.
- The real fix (extending `Runtime` for cancellation) is tracked in a separate issue and
  needs the same explicit go-ahead any other module-boundary change does.
