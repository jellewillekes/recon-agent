# recon-agent

An agent evaluation platform for financial research tasks. The agent answers analyst
questions using tools; the harness measures how well it does that — not just the
answer, but the path taken to reach it.

The harness is the product, not the agent. See `AGENTS.md` for project conventions,
`docs/contracts.md` for the module boundaries, and
[`docs/implementation-plan.md`](docs/implementation-plan.md) for the step-by-step
build plan this repo follows.

## Status

Steps 1-10 of the implementation plan are done: contracts, the finance-agent-bench
dataset adapter, the MCP tool server, the FastAPI service (`recon.api.main`), the
evaluation harness (`recon.cli eval`), guardrails and reliability (prompt-injection
tests, the review-flag write path, tool-layer retries and budgets), two runtimes (the
Agent SDK and LangGraph, each with a single and a multi-agent mode), and
containerization/orchestration (`docker/`, `charts/recon-agent/`, running locally on
k3d — see [`docs/deployment.md`](docs/deployment.md)). See
[`docs/runtimes.md`](docs/runtimes.md) for how the two runtimes compare.

Since then, the tools answer from real SEC EDGAR data instead of a synthetic fixture
([`docs/data-sources.md`](docs/data-sources.md)), and the promotion gate refuses to
compare runs that don't measure the same thing (`docs/adr/0018-run-comparability-in-the-gate.md`).
CI also builds the image, scans it with Trivy and lints the chart, and a version tag
publishes the image to GHCR ([`docs/ci.md`](docs/ci.md)).
The first baseline is recorded on a 7-case smoke set (`evals/baseline.json`; see the table
below). Eval runs are capped at €1 by default (`docs/adr/0021-eval-cost-controls.md`).

## Runtimes

Four `--runtime {sdk,langgraph} --mode {single,multi}` combinations run against the
same dataset. See [`docs/runtimes.md`](docs/runtimes.md) for what primitives each one
offers and where they differ.

| Runtime | Mode | Task completion | Answer score | Tool-call accuracy | Total cost (€) | Cases |
|---|---|---|---|---|---|---|
| sdk | single | 1.000 | 0.566 | 1.000 | 0.68 | 7 (smoke set) |
| sdk | multi | — (pending) | — (pending) | — (pending) | — (pending) | — |
| langgraph | single | — (pending) | — (pending) | — (pending) | — (pending) | — |
| langgraph | multi | — (pending) | — (pending) | — (pending) | — (pending) | — |

The sdk single row is the committed baseline, `evals/baseline.json` (run
`eval-20261001T085035Z`, judge on Haiku 4.5). Seven cases is a small sample: one case
moves a mean by 0.14.

Populated from real `recon.cli eval` runs, not placeholders — held pending explicit
go-ahead per `AGENTS.md`'s Cost section. Verify command:
`uv run python -m recon.cli eval --runtime <sdk|langgraph> --mode <single|multi> --cases evals/smoke-cases.txt`.
Each run is capped at €1 by default (`--max-cost-eur`); see `docs/adr/0021-eval-cost-controls.md`.

## Setup

```bash
uv sync
uv run pytest
```

## Repo automation

Issues in this repo can be implemented and opened as PRs by Claude, and those PRs
are then reviewed — and iterated on — by a separate Claude review agent before a
human merges. This is repo tooling, not the investigator agent under evaluation.
See [`docs/github-agents.md`](docs/github-agents.md) for how the agents are
wired together, how they communicate, and where a human is required to step in.
