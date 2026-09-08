# recon-agent

An agent evaluation platform for financial research tasks. The agent answers analyst
questions using tools; the harness measures how well it does that — not just the
answer, but the path taken to reach it.

The harness is the product, not the agent. See `CLAUDE.md` for project conventions,
`docs/contracts.md` for the module boundaries, and
[`docs/implementation-plan.md`](docs/implementation-plan.md) for the step-by-step
build plan this repo follows.

## Status

Steps 1-9 of the implementation plan are done: contracts, the finance-agent-bench
dataset adapter, the synthetic-fixture MCP tool server, the FastAPI service
(`recon.api.main`), the evaluation harness (`recon.cli eval`), guardrails and
reliability (prompt-injection tests, the review-flag write path, tool-layer retries
and budgets), and two runtimes (the Agent SDK and LangGraph), each with a single and
a multi-agent mode. See [`docs/runtimes.md`](docs/runtimes.md) for how the two
runtimes compare. No baseline score is recorded yet — that's a separate, explicitly-
requested run (see `docs/implementation-plan.md`).

## Runtimes

Four `--runtime {sdk,langgraph} --mode {single,multi}` combinations run against the
same dataset. See [`docs/runtimes.md`](docs/runtimes.md) for what primitives each one
offers and where they differ.

| Runtime | Mode | Task completion | Answer score | Tool-call accuracy | Total cost (€) | Cases |
|---|---|---|---|---|---|---|
| sdk | single | — (pending) | — (pending) | — (pending) | — (pending) | — |
| sdk | multi | — (pending) | — (pending) | — (pending) | — (pending) | — |
| langgraph | single | — (pending) | — (pending) | — (pending) | — (pending) | — |
| langgraph | multi | — (pending) | — (pending) | — (pending) | — (pending) | — |

Populated from real `recon.cli eval` runs, not placeholders — held pending explicit
go-ahead per `CLAUDE.md`'s Cost section. Verify command:
`uv run python -m recon.cli eval --runtime <sdk|langgraph> --mode <single|multi> --limit N`.

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
