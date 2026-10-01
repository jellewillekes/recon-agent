"""OpenTelemetry tracing for eval runs and the API (step 12, #17).

Off unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set: then spans go over OTLP/HTTP
to that endpoint, e.g. the compose stack's Tempo at http://localhost:4318.
Without it the tracer is OpenTelemetry's no-op, so tests, CI and normal runs
pay nothing. Span and attribute names follow the GenAI semantic conventions
where one exists (`gen_ai.*`); project-specific ones use `recon.*`. See
docs/observability.md and docs/adr/0023-tracing-at-the-harness-boundary.md.

Spans are made where an `AgentResult` or judge result comes back, not inside
each runtime. Tool calls come back as a list with durations but no start
times, so their spans are laid out one after another from the start of the
agent span, marked `recon.timing=sequential`.
"""

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import Span, Status, StatusCode

from recon.contracts import AgentResult

ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
# Tool statuses that are answers, not failures (docs/contracts.md section 3).
_TOOL_OUTCOMES = frozenset({"ok", "empty", "truncated"})
_tracer = trace.get_tracer("recon")


def configure_tracing(service_name: str, exporter: SpanExporter | None = None) -> bool:
    """Install a tracer provider exporting to `exporter`, or to OTLP/HTTP when
    `OTEL_EXPORTER_OTLP_ENDPOINT` is set. Returns whether tracing is on.

    Call once at process start. Tests pass an in-memory exporter.
    """
    if exporter is None:
        if not os.environ.get(ENDPOINT_ENV):
            return False
        # Imported here so a run without tracing never loads the exporter.
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        exporter = OTLPSpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return True


def shutdown_tracing() -> None:
    """Flush pending spans. A CLI run must call this before it exits."""
    provider = trace.get_tracer_provider()
    shutdown = getattr(provider, "shutdown", None)
    if shutdown is not None:
        shutdown()


@contextmanager
def span(name: str, **attributes: str | float | bool) -> Iterator[Span]:
    """A span named `name`, current for the `with` block."""
    with _tracer.start_as_current_span(name, attributes=attributes) as current:
        yield current


def record_agent_result(
    agent_span: Span, result: AgentResult, model: str | None = None
) -> None:
    """Put an agent run's usage on its span, and add a child span per tool call."""
    agent_span.set_attributes(
        {
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.provider.name": "anthropic",
            "gen_ai.usage.input_tokens": result.tokens_in,
            "gen_ai.usage.output_tokens": result.tokens_out,
            "recon.cost_eur": result.cost_eur,
            "recon.runtime": result.runtime,
            "recon.mode": result.mode,
            "recon.tool_call_count": len(result.tool_calls),
        }
    )
    if model is not None:
        agent_span.set_attribute("gen_ai.request.model", model)
    if result.error is not None:
        agent_span.set_attribute("error.type", "agent_error")
        if not result.answer.strip():
            agent_span.set_status(Status(StatusCode.ERROR, result.error))
    _record_tool_calls(agent_span, result)


def _record_tool_calls(agent_span: Span, result: AgentResult) -> None:
    start_ns = getattr(agent_span, "start_time", None) or time.time_ns()
    context = trace.set_span_in_context(agent_span)
    for call in result.tool_calls:
        end_ns = start_ns + call.elapsed_ms * 1_000_000
        tool_span = _tracer.start_span(
            f"execute_tool {call.tool}",
            context=context,
            start_time=start_ns,
            attributes={
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": call.tool,
                "recon.tool.status": call.status,
                "recon.timing": "sequential",
            },
        )
        if call.status not in _TOOL_OUTCOMES:
            tool_span.set_attribute("error.type", call.status)
            tool_span.set_status(Status(StatusCode.ERROR, call.status))
        tool_span.end(end_time=end_ns)
        start_ns = end_ns


def record_judge_call(
    judge_span: Span,
    *,
    model: str,
    tokens_in: int,
    tokens_out: int,
    num_turns: int,
    cost_eur: float,
) -> None:
    """Put a judge call's usage on its span."""
    judge_span.set_attributes(
        {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": "anthropic",
            "gen_ai.request.model": model,
            "gen_ai.usage.input_tokens": tokens_in,
            "gen_ai.usage.output_tokens": tokens_out,
            "recon.num_turns": num_turns,
            "recon.cost_eur": cost_eur,
        }
    )
