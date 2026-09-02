# CLAUDE.md

Project conventions. Read this fully before changing anything.

## What this project is

An agent evaluation platform for financial research tasks. The agent answers analyst questions using tools; the harness measures how well it does that — not just the answer, but the path taken to reach it.

**The harness is the product, not the agent.** A harness that runs against two datasets and two runtimes is the goal. The agent is the thing being measured.

## Repository layout

```
src/recon/
  contracts.py     Pydantic models for every module boundary
  adapters/        external data formats -> internal schema
  tools/           MCP server and tool implementations
  runtimes/        agent runtimes behind one protocol
  eval/            harness, metrics, rubrics
  api/             FastAPI service
  cli.py
prompts/           system prompts, one file per role
config/            YAML configuration, rubrics, role tool subsets
data/              gitignored except README
docker/            Dockerfile, compose.yaml
charts/            Helm chart
docs/              contracts.md, deployment.md, adr/
evals/results/     evaluation artifacts — these ARE committed
tests/
.github/workflows/
```

Do not change this layout without asking.

## Storage boundary

Two stores, deliberately. Do not consolidate them.

- **DuckDB** — read-only analytical queries over Parquet and CSV. Used by tools. In-process, no server.
- **Postgres** — transactional state: LangGraph checkpoints, the review-flag table, pgvector embeddings. Concurrent writers, unique constraints, transactions.

DuckDB has a single writer per file and breaks under concurrent runs. Postgres is not a query engine for columnar analytics. Use each for what it is.

## Commands

```bash
uv run pytest                       # all tests
uv run pytest -m "not llm"          # tests without LLM calls (what CI runs)
uv run ruff check --fix && uv run ruff format
uv run python -m recon.cli eval     # evaluation — consumes credit
uv run uvicorn recon.api.main:app --reload
docker compose -f docker/compose.yaml up -d
helm lint charts/recon-agent
```

## Definition of done

A step is done only when **all** of these hold:

1. `uv run pytest` is green — you ran it; do not report done based on reading the code
2. `uv run ruff check` is clean
3. The step's verification command was executed and the output matches
4. New public functions have type hints and a docstring
5. No TODOs or `pass` stubs left behind

## Forbidden without explicit permission

- **Filling in, generating, or modifying the golden set or expected answers.** Labels come from the external dataset or from the user. Never from you
- Lowering an evaluation threshold because a test fails
- Adding dependencies — proposing is fine, installing needs approval
- Changing the repository layout or module boundaries
- Putting prompts inline in Python; they belong in `prompts/`
- Network calls in tests. Mock them
- Naming individual companies in code, commits, docs, or prompt text. Describe market participants by category and role. Datasets, tools, and APIs we integrate with may be named where technically necessary
- Secrets or API keys in code or config. Environment variables only

## Style

- Python 3.12, type hints throughout, Pydantic for anything crossing a boundary
- Functions under 50 lines, modules under 400. Split rather than extend
- No bare `except`. Catch specifically and log with context
- No comments restating the code. Comments explaining a decision, yes
- Error messages say what went wrong **and** what the caller can do about it

## Tests

- Mark anything that calls a model with `pytest.mark.llm`. Those do not run in CI
- Tool contract tests run without an LLM and cover every error case in `docs/contracts.md`
- New behaviour gets a test in the same PR

## Evaluations and CI

LLM evaluations run **locally**, not in CI — GitHub Actions has no model credentials. The flow:

1. Run `recon.cli eval` locally
2. Commit the result under `evals/results/`
3. CI verifies the artifact exists and clears the threshold in `config/thresholds.yaml`

Do not try to make CI call a model.

## Git

- Conventional commits: `feat:`, `fix:`, `test:`, `docs:`, `chore:`
- One step, one PR. Do not combine two steps
- Never commit directly to `main`

## Voice

Applies to anything a Claude agent writes for a human to read in this repo:
review comments, PR descriptions, commit messages, issue bodies.

- One idea per sentence. If a sentence needs a semicolon or a second em dash
  to finish, split it into two sentences
- At most one em dash per paragraph
- Don't narrate your own session's tool constraints ("my permissions didn't
  extend to X", "I wasn't able to run Y in this session"). If a tool was
  missing, say what you did instead, in one clause
- Don't repeat the same justification shape on every bullet (e.g. "Covered
  by test X" appended to five findings in a row). Say it once, or fold it
  into the finding itself
- Lead with the finding in one plain sentence. Justify in at most one more
  sentence, not three stacked clauses defending a single claim
- Plain sentence-case lead-ins, not bolded pseudo-headers imitating a report
  ("Main concern —", "Genuine question, not a defect")

Before, from an actual review comment on this repo:

> fetch_csv treats "a file exists at dest" as "cache is valid," full stop —
> it never checks that cache against PINNED_COMMIT. But docs/data-sources.md
> explicitly documents the workflow as "bump the pin deliberately, in its own
> PR, if the upstream file changes," which implies that bumping the constant
> is how you get fresh data. In practice: a developer or CI runner that
> already has data/raw/finance_agent_bench/public.csv cached from before a
> future pin bump will keep silently serving the stale file — nothing in
> this code path ever notices the pin moved, and there's no error, warning,
> or log.

After:

> fetch_csv only checks whether dest exists — it never checks that file
> against PINNED_COMMIT. After a future pin bump, anyone with an old cached
> file keeps serving it silently. No error, no warning.

## Cost

The runtime uses the Agent SDK credit on a personal subscription, not an API key. Every evaluation run consumes credit.

- Never run a full evaluation unless the user asks for it
- Use `--limit 3` when testing harness changes
- Report tokens and cost in every evaluation result

## When unsure

Ask. Do not assume. A wrong assumption about a data contract costs more than a question.
