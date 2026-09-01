# recon-agent

An agent evaluation platform for financial research tasks. The agent answers analyst
questions using tools; the harness measures how well it does that — not just the
answer, but the path taken to reach it.

The harness is the product, not the agent. See `CLAUDE.md` for project conventions
and `docs/contracts.md` for the module boundaries.

## Status

Scaffolding only (step 0 of `implementation-plan.md`). No contracts, tools, or
runtimes implemented yet.

## Setup

```bash
uv sync
uv run pytest
```
