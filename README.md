# recon-agent

An agent evaluation platform for financial research tasks. The agent answers analyst
questions using tools; the harness measures how well it does that — not just the
answer, but the path taken to reach it.

The harness is the product, not the agent. See `CLAUDE.md` for project conventions,
`docs/contracts.md` for the module boundaries, and
[`docs/implementation-plan.md`](docs/implementation-plan.md) for the step-by-step
build plan this repo follows.

## Status

Steps 1-5 of the implementation plan are done: contracts, the finance-agent-bench
dataset adapter, the synthetic-fixture MCP tool server, the Agent SDK runtime, and the
evaluation harness (`recon.cli eval`) all run end to end. No baseline score is recorded
yet — that's a separate, explicitly-requested run (see `docs/implementation-plan.md`).

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
