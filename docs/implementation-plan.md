# Implementation plan — executable steps, local and free

One Claude Code session per step, one PR, one verification command with an expected outcome.

Prerequisite: `CLAUDE.md` in the repo root and `docs/contracts.md` in `docs/` before step 1.

---

## Where it runs

| Layer | Choice |
|---|---|
| Dev stack | Docker Compose — Postgres with pgvector, Grafana, Tempo, Prometheus, Ollama |
| Kubernetes | k3d (a real k3s cluster inside Docker) |
| Packaging | Helm chart |
| CI/CD | GitHub Actions, free tier on a public repo |
| Agent runtime | Claude Agent SDK on subscription credit |
| Evaluations | Local; results committed as artifacts, CI checks the threshold |

No cloud account needed. Everything you build runs unchanged on a managed cluster.

---

## Step 0 — setup (20 min, manual)

```bash
mkdir recon-agent && cd recon-agent
git init && uv init --python 3.12
uv add claude-agent-sdk mcp duckdb polars pydantic pytest ruff httpx pyyaml
mkdir -p src/recon/{adapters,tools,runtimes,eval,api} prompts config/rubrics \
         docs/adr evals/results tests data docker charts .github/workflows
# place CLAUDE.md and docs/contracts.md, commit them first
echo $ANTHROPIC_API_KEY   # MUST be empty
brew install k3d helm     # or the equivalent for your platform
```

Claim the Agent SDK credit in your Claude account. Turn off extra usage.

---

## Step 1 — contracts in code (45 min)

> Implement `src/recon/contracts.py` exactly as specified in `docs/contracts.md`: `Case`, `ToolResult`, `ToolCall`, `AgentResult`, `ReviewFlag`, `CaseScore`, `EvalRun` as Pydantic v2 models with validators. Add `tests/test_contracts.py` verifying per model that required fields are required, that a `Case` with neither `expected_answer` nor `expected_tool_path` is rejected, and that each `ToolResult.status` enforces the corresponding `data` behaviour. Add a test asserting that every field documented in `docs/contracts.md` exists in the models.

**Verify:** `uv run pytest tests/test_contracts.py -q` → at least 12 tests passing.

---

## Step 2 — load the dataset (1 hour)

> Fetch the public validation set of the finance agent benchmark from `github.com/vals-ai/finance-agent`. **Inspect the actual CSV schema and report it back to me before writing any code.**
>
> Then write `src/recon/adapters/finance_agent_bench.py` mapping that CSV to `Case` objects. Map and validate only — never interpret or fill in. Populate `license` and `attribution` from the source. Add `docs/data-sources.md` recording licence, DOI and required attribution text.
>
> Add `recon.cli dataset --stats` reporting case count, distribution across `tags`, and how many cases carry an `expected_tool_path`.

**Verify:** `uv run python -m recon.cli dataset --stats` → table with case count and tag distribution.

---

## Step 3 — MCP tools (2 hours)

> Build `src/recon/tools/server.py` as an MCP server over stdio. **Determine which tools the dataset requires from the `context` fields of the loaded cases and propose them to me before implementing.**
>
> Every tool returns `ToolResult` and implements all five statuses from `docs/contracts.md`. `MAX_ROWS = 500`, `TIMEOUT_S = 30`. Pydantic input schemas, and tool descriptions that make clear when to use the tool and when not to. Queries go through DuckDB, read-only.
>
> `tests/test_tools.py` calls each tool directly with no LLM and covers all five statuses per tool. Network mocked.

**Verify:** `uv run pytest tests/test_tools.py -q` → at least 5 tests per tool.

---

## Step 4 — agent, one runtime (1.5 hours)

> Define `src/recon/runtimes/base.py` with a `Runtime` protocol: `run(case: Case) -> AgentResult`. Implement `runtimes/agent_sdk.py` using the Claude Agent SDK against the MCP server from step 3. System prompt in `prompts/investigator.md`, loaded by path, never inline.
>
> Populate `tokens_in`, `tokens_out`, `cost_eur` from the SDK response, and `tool_calls` in call order. On failure: populate `error`, do not raise to the caller.
>
> `recon.cli run --case-id X` runs one case and prints the `AgentResult` as JSON.

**Verify:** `uv run python -m recon.cli run --case-id <first case>` → valid JSON with at least one tool call and populated cost.

---

## Step 5 — evaluation harness (2.5 hours) → **POC**

> Build `src/recon/eval/`. Metrics per `docs/contracts.md`: task completion, answer score from rubric, tool path exact and equivalent, tool-call accuracy, cost, latency.
>
> Rubrics from `config/rubrics/*.yaml` as assertions, not free-form judgement. Write at least three: `answer_correctness`, `evidence_grounding`, `tool_efficiency`.
>
> Compute `prompt_hashes` from the files in `prompts/` and `model_config_hash` from `config/models.yaml`. Write `EvalRun` to `evals/results/<run_id>.json` plus a markdown summary.
>
> `recon.cli eval [--limit N] [--baseline evals/baseline.json]`, exiting non-zero on regression per the promotion gate rules.

**Verify:** `uv run python -m recon.cli eval --limit 3` → JSON and markdown in `evals/results/`, all metrics populated, `prompt_hashes` non-empty.

Then run once in full, record the result as the baseline, and put your score next to the published reference figure in the README.

---

## Step 6 — FastAPI service (1 hour)

Covers *API development, microservices* from the requirements, and makes the Helm chart in step 10 meaningful.

> Build `src/recon/api/` with FastAPI, implementing the API contract in `docs/contracts.md`: `POST /investigate`, `GET /healthz`, `GET /readyz`, `GET /metrics`. `/healthz` performs no dependency checks; `/readyz` checks the MCP server and Postgres.
>
> Async, with a concurrency limit and a per-request timeout. Request ID carried into the agent and returned in the response header. Configuration via environment variables, twelve-factor style, no hardcoded paths.
>
> `tests/test_api.py` using `httpx.AsyncClient` with the agent mocked, covering success, timeout and error paths.

**Verify:** `uv run uvicorn recon.api.main:app` then `curl localhost:8000/healthz` → 200. `/docs` renders the OpenAPI spec.

---

## Step 7 — multi-agent (2 hours)

> Extend `runtimes/agent_sdk.py` with a multi-agent mode using subagents: a supervisor that decomposes and routes, two specialised workers each with their own tool subset, and a critic that checks the conclusion against `evidence` and rejects a conclusion without support.
>
> Tool restriction per role in `config/roles.yaml`, enforced when attaching the MCP server — not in the prompt text. Prompts per role in `prompts/`.
>
> Add `--mode single|multi` to `run` and `eval`. The harness stays unchanged.

**Verify:** `uv run python -m recon.cli eval --mode multi --limit 3` runs. A test proves a worker cannot call a tool outside its subset.

Run both modes in full. **If single wins, publish that.**

---

## Step 8 — guardrails and reliability (2 hours)

> Three things.
>
> A prompt-injection test suite: instructions hidden in tool output, with assertions that the agent does not leave its tool subset and does not follow the instruction. Fails hard on a successful injection.
>
> The `flag_case_for_review` write path per `docs/contracts.md`: Postgres, idempotency key with a unique constraint, dry-run mode, confirmation step that pauses the run. A test calls it twice and expects one row.
>
> Production semantics in the tool layer: retry with exponential backoff and jitter, a circuit breaker returning `unavailable` after three consecutive failures, and per-run budget enforcement on tool calls, tokens and wall-clock time with graceful termination and a partial result.

**Verify:** `uv run pytest tests/test_injection.py tests/test_reliability.py -q` green, chaos tests included.

---

## Step 9 — LangGraph as a second runtime (2 hours)

The largest gap against the target role, and cheap because the tools live in MCP.

> Implement `runtimes/langgraph.py` behind the same `Runtime` protocol, against the same MCP server via `langchain-mcp-adapters`. Single mode as a ReAct graph, multi mode as a supervisor graph using the same roles and tool subsets from `config/roles.yaml`.
>
> Explicit `AgentState` TypedDict, Postgres checkpointer, human-in-the-loop interrupt before `flag_case_for_review`.
>
> Add `--runtime sdk|langgraph` to `run` and `eval` so four combinations run against the same dataset. Document in `docs/runtimes.md` which primitives each runtime offers and where they differ.

**Verify:** `uv run python -m recon.cli eval --runtime langgraph --mode multi --limit 3` runs; the README carries a table of all four combinations.

---

## Step 10 — Docker, k3d and Helm (1.5 hours)

Covers *containerization (Docker) and basic orchestration (Kubernetes)*.

> Multi-stage `docker/Dockerfile`: builder using uv, runtime on distroless or slim, non-root user, no build tools in the final layer. Image under 300 MB.
>
> `docker/compose.yaml` for the dev stack: Postgres with pgvector, Grafana, Tempo, Prometheus, Ollama, and the API service.
>
> `charts/recon-agent/` as a Helm chart: deployment, service, configmap, secret, resource requests and limits, liveness and readiness probes pointing at `/healthz` and `/readyz`, and a `values.yaml` documenting every setting. `helm lint` must be clean.
>
> `docs/deployment.md` describes what changes when deploying to a managed cluster instead of k3d — ingress, secrets, image registry, OTLP endpoint.

**Verify:**
```bash
k3d cluster create recon
docker build -t recon-agent:dev -f docker/Dockerfile .
k3d image import recon-agent:dev -c recon
helm install recon charts/recon-agent
kubectl wait --for=condition=ready pod -l app=recon-agent --timeout=120s
```
→ pod ready; `kubectl port-forward` then `curl /healthz` returns 200.

---

## Step 11 — CI/CD (1 hour)

Covers *CI/CD pipelines, production deployment*.

> `.github/workflows/ci.yaml`: ruff check and format check, pytest excluding the `llm` marker, docker build, `trivy` image scan failing on HIGH and CRITICAL, `helm lint`, and a job verifying that `evals/baseline.json` exists and clears the threshold in `config/thresholds.yaml`.
>
> `.github/workflows/release.yaml`: on tag, build and push the image to GHCR with digest pinning and an SBOM as an artifact.
>
> Pre-commit with ruff, gitleaks, and a hook that fails when a prompt file changes without `evals/baseline.json` being regenerated.
>
> Document in `docs/ci.md` why LLM evaluations run locally rather than in CI.

**Verify:** push a branch → all jobs green. A deliberately degraded prompt without a new baseline makes the pre-commit hook fail.

---

## Step 12 — observability (1.5 hours)

> OpenTelemetry tracing using the GenAI semantic conventions: a span per LLM call and per tool call, carrying model, tokens, cost, tool name and status as attributes. HTTP spans from FastAPI sharing the same request ID. Export via OTLP to the Compose stack. Grafana dashboard showing runs, cost per run, latency distribution and tool errors by type.

**Verify:** `docker compose -f docker/compose.yaml up -d`, then an eval run → traces visible in Grafana with a span per tool call.

---

## Step 13 — RAG (2 hours)

> Add a local retrieval layer. Corpus in `data/knowledge/` as markdown. Chunking with overlap, embeddings via `sentence-transformers` locally, storage in **pgvector** in the existing Postgres. Hybrid search combining `rank_bm25` with dense retrieval via reciprocal rank fusion, plus reranking with a local cross-encoder.
>
> Expose as the MCP tool `search_knowledge(query, top_k)`, available to supervisor and critic, not to workers.
>
> Add retrieval metrics to the harness: context precision, context recall and faithfulness, against manually labelled relevant chunks for five cases.

**Verify:** `uv run pytest tests/test_retrieval.py -q` green; new metrics appear in `EvalRun.aggregate`.

---

## Step 14 — provider abstraction (1.5 hours)

> A `ModelProvider` protocol with two implementations: the Agent SDK on subscription, and a local Ollama provider. Configuration per role in `config/models.yaml`.
>
> Model routing: the local model handles routing, classification and summarising tool output; the strong model handles the final conclusion and the critic. Add `--routing on|off`, and have the harness report cost and task completion for both settings.

**Verify:** `uv run python -m recon.cli eval --routing on --limit 3` shows lower cost; the effect on task completion goes in the README.

---

## Overview

| Step | Hours | Cumulative | Coverage |
|---|---|---|---|
| 0–4 | 5.75 | 5.75 | — |
| **5 — POC** | 2.5 | **8.25** | ~45% |
| 6 — FastAPI | 1 | 9.25 | ~52% |
| 7 — multi-agent | 2 | 11.25 | ~59% |
| 8 — guardrails | 2 | 13.25 | ~67% |
| 9 — LangGraph | 2 | 15.25 | ~74% |
| 10 — k3d + Helm | 1.5 | 16.75 | ~81% |
| 11 — CI/CD | 1 | 17.75 | ~85% |
| 12 — observability | 1.5 | 19.25 | ~89% |
| 13 — RAG | 2 | 21.25 | ~93% |
| 14 — providers | 1.5 | 22.75 | ~93% |

## The one gap that remains

Hands-on cloud platform experience cannot be demonstrated here without an account. Two things stand against that.

You already have it elsewhere — your existing platform runs on GCP with Terraform, Cloud Run, BigQuery and Workload Identity Federation. Two repos, two pieces of evidence. Say it that way.

And this project is cloud-ready without cloud: the Helm chart, OTLP export and twelve-factor config run unchanged on GKE or AKS. `docs/deployment.md` turns the absence of an account into a cost decision rather than a knowledge gap.

## Do not delegate

- Deciding what happens when the evaluation returns a surprising result. If multi-agent loses to single, that is a finding, not a bug to engineer away
- What you descope, and why
- Replacing the baseline
- Setting rubric assertions — that is your judgement about what a good answer looks like

## Order

Steps 1 through 5 are the core. Start there and do not stop until `eval` runs. After that, steps 6 and 10 together are the cheapest jump in coverage: 2.5 hours for three requirements.

If you get no further than step 5, you still have an agent with a serious evaluation harness — rarer than a multi-agent demo without one.
