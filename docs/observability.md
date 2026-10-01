# Observability

Traces show where a run's time and money go: per case, per agent run, per judge
call and per tool call. Step 12 (#17). The design is in
`docs/adr/0023-tracing-at-the-harness-boundary.md`.

## Turn it on

Tracing is off unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set. Then `recon.cli eval`
(service `recon-eval`) and the API (service `recon-api`) export spans over OTLP/HTTP.

```bash
docker compose -f docker/compose.yaml up -d tempo grafana
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \
  uv run python -m recon.cli eval --cases evals/smoke-cases.txt
```

Open Grafana at http://localhost:3000. The provisioned dashboard is under
**recon-agent → recon-agent: eval runs**. Tests clear the variable, so they never
export.

## Spans

| Span | Parent | Attributes |
|---|---|---|
| `eval.run` | — | `recon.run_id`, runtime, mode, `recon.total_cost_eur`, every aggregate metric as `recon.<name>` |
| `eval.case` | `eval.run` | `recon.case_id`, `recon.answer_score`, `recon.task_completion`, `recon.cost_eur` |
| `invoke_agent` | `eval.case` or the API request | `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.request.model` (single mode), `recon.cost_eur`, `recon.tool_call_count` |
| `execute_tool <tool>` | `invoke_agent` | `gen_ai.tool.name`, `recon.tool.status`, `error.type` on a failure |
| `chat judge` | `eval.case` | model, tokens, `recon.num_turns`, `recon.cost_eur` |
| `POST /investigate` etc. | — | `recon.request_id` (the `X-Request-ID` header), method, path, status code |

Names follow the OpenTelemetry GenAI semantic conventions where one exists.

- Tool spans are placed one after another from the start of the agent span. Runtimes
  report each call's duration but not its start, so the gaps between calls (model
  thinking time) aren't shown. They carry `recon.timing=sequential`.
- `empty` and `truncated` are tool answers, not failures. Only `invalid_input`,
  `unavailable` and unknown statuses mark a tool span as an error.
- In multi mode, one `invoke_agent` span covers the supervisor, workers and critic
  together, and no model is named.

## Dashboard

All panels query Tempo with TraceQL. Tempo 3 serves TraceQL metrics without a
metrics generator.

- **Eval runs:** a table of runs with case count, answer score and cost split.
- **Cost per run** and **agent vs judge cost**, in EUR.
- **Case latency:** p50 and p90 of `eval.case`.
- **Tool errors** by tool and error type.

Prometheus keeps scraping the API's `/metrics` (request counts, latency, in-flight
runs). Traces don't replace that.
