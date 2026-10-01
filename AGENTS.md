# AGENTS.md

Project conventions for any coding agent. Claude Code reads this file through
`CLAUDE.md`, and other agents read it directly. Read it fully before changing
anything. Where code or docs refer to "CLAUDE.md's" sections, they mean the
sections here.

<!-- rules:start -->
## How to work

- State your assumptions. If a request can be read more than one way, list the readings; don't pick one silently.
- If ambiguity blocks you, ask one question at a time and include your recommended answer.
- If something is unclear or contradicts itself, say exactly what confuses you, then stop.
- Write the minimum code that solves the task. No speculative features, config options or abstractions.
- Touch only what the task needs. Don't reformat, rename or clean up unrelated code.
- Before you start, define "done" as a check you can run. When you finish, show its output.
- Solve the general case. Never hardcode to test inputs or edit tests to make them pass; if a test looks wrong, say so.
- Read a file before making claims about it.
- When you finish, list any shortcuts, skipped cases or TODOs you left behind.
<!-- rules:end -->

These rules come from the agentic-framework repo; see `docs/adr/0017-claude-code-project-setup.md`.

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
.claude/           Claude Code settings, hooks, path-scoped rules, subagents
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
make check                          # format, lint, types, tests without LLM calls (what CI runs)
make fix                            # format + safe autofix
uv run pytest                       # all tests, including llm-marked ones that spend credit
uv run python -m recon.cli eval --cases evals/smoke-cases.txt   # evaluation — consumes credit, capped at €1
uv run uvicorn recon.api.main:app --reload
cp docker/.env.example docker/.env  # once, then fill in the passwords
docker compose -f docker/compose.yaml up -d
helm lint charts/recon-agent
```

## Definition of done

A step is done only when **all** of these hold:

1. `make check` is green (format, lint, types, and `pytest -m "not llm"`, the same as CI) — you ran it; do not report done based on reading the code. `llm`-marked tests spend credit and run only with the user's go-ahead
2. The step's verification command was executed and the output matches
3. New public functions have type hints and a docstring
4. No TODOs or `pass` stubs left behind

## Forbidden without explicit permission

- **Filling in, generating, or modifying the golden set or expected answers.** Labels come from the external dataset or from the user. Never from you
- Lowering an evaluation threshold because a test fails
- Adding dependencies — proposing is fine, installing needs approval
- Changing the repository layout or module boundaries
- Putting prompts inline in Python; they belong in `prompts/`
- Network calls in tests. Mock them
- Secrets or API keys in code or config. Environment variables only

## Style

- Python 3.12, type hints throughout, Pydantic for anything crossing a boundary
- Functions under 50 lines, modules under 400. Split rather than extend
- No bare `except`. Catch specifically and log with context
- No comments restating the code. Comments explaining a decision, yes
- Error messages say what went wrong **and** what the caller can do about it
- New behaviour gets a test in the same PR

## Evaluations and CI

LLM evaluations run **locally**, not in CI — GitHub Actions has no model credentials. Run `recon.cli eval` locally and commit the result under `evals/results/`. CI checks that `prompts/` still matches `evals/baseline.json`. CI also checks that the baseline is usable and clears the minimums in `config/thresholds.yaml`. Until `evals/baseline.json` exists, both checks only print a notice. Do not try to make CI call a model. See `docs/ci.md`.

## Git

- Conventional commits: `feat:`, `fix:`, `test:`, `docs:`, `chore:`
- One step, one PR. Do not combine two steps
- Never commit directly to `main`

## Architecture decisions

When a design decision has more than one reasonable option and gets settled — by the user,
or in a terminal/session discussion — record it in `docs/adr/` as a new numbered file
(`000X-short-title.md`): Context (what's being decided and why it isn't obvious),
Decision, Consequences. Keep it short — a few paragraphs, not a design doc. A PR description
should point back to the ADR rather than re-arguing the reasoning inline. Write one whether
the decision came from the user directly or from a Claude session's own investigation and
choice — both count.

## Voice

Applies to anything a Claude agent writes for a human to read in this repo:
review comments, PR descriptions, commit messages, issue bodies. Worked
before/after examples are in `docs/voice-examples.md`.

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

### Reviews specifically

- One finding, one bullet. Never blend two concerns into the same paragraph
- Bullet shape: `file:line` first, then the finding in one sentence, then at
  most one more sentence for why it matters or what to do. Two sentences max
- Tag each finding's severity at the start of the bullet: `Blocking:`,
  `Question:`, or `Note:` (non-blocking observation)
- Anything that anchors to a line is an inline comment, not review-body
  prose. The body holds only a short overall summary plus, from round 2
  onward, a status list — never a restatement of each inline comment
- Round 2+ review bodies open with a status list, one line per item:
  `Open:`, `Resolved:`, `New:`. No prose recap paragraph before it

## Cost

The runtime uses the Agent SDK credit on a personal subscription, not an API key. Every evaluation run consumes credit.

- Never run a full evaluation unless the user asks for it
- Use `--cases evals/smoke-cases.txt` (7 cases, about €0.35) or `--limit 3` when testing harness changes
- `eval` refuses to start above `--max-cost-eur` (€1 by default) and stops before a case that could pass it. Don't raise the cap without the user's go-ahead
- Report tokens and cost in every evaluation result

## When unsure

Ask. Do not assume. A wrong assumption about a data contract costs more than a question.
