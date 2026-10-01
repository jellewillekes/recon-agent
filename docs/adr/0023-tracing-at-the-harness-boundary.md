# ADR 0023: Tracing at the harness boundary

Status: Accepted
Date: 2026-10-01

## Context

Step 12 (#17) asks for a span per model call and per tool call, with tokens, cost
and status. The three runtimes (Agent SDK, LangGraph, multi-agent) each run their own
message loop, and those modules are already 300–770 lines. The Agent SDK also hides
individual model calls: one `query()` reports usage once, at the end. The user wants
cost visible per call, starting with the judge, which takes about a quarter of a
run's cost (ADR 0021).

## Decision

- Spans are made in the harness and the API, where an `AgentResult` or a judge result
  comes back, not inside each runtime. `recon/tracing.py` holds the setup and the
  recording functions. Runtimes are unchanged.
- One `invoke_agent` span per agent run carries its tokens and cost, and one
  `chat judge` span per judge call. The judge result now returns its tokens, turns
  and model for that.
- Tool spans are built from `AgentResult.tool_calls`, placed one after another from
  the agent span's start, because the contract records durations but not start times.
  They're marked `recon.timing=sequential`.
- Tracing is off unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set. The user approved three
  dependencies: `opentelemetry-api`, `-sdk` and `-exporter-otlp-proto-http`. FastAPI
  spans come from the existing request-ID middleware rather than an instrumentation
  package.
- The dashboard queries Tempo with TraceQL metrics. That needs no extra metrics
  pipeline, because Tempo 3 serves them by default.

## Consequences

- No runtime changes, and the same spans for every runtime.
- Tool timing within an agent span is approximate. Real start times would need a new
  optional field on `ToolCall`, a contract change.
- A multi-mode run shows as one agent span. Per-role spans would need tracing inside
  `runtimes/multi_agent.py` and `runtimes/langgraph_multi.py`.
