# ADR 0020: Company names may appear in the repo

Status: Accepted
Date: 2026-10-01

## Context

AGENTS.md forbade naming individual companies in code, commits, docs and prompt text.
Issue #76 proposed enforcing that with a denylist check in CI. While reviewing that
check in PR #84, the user decided the rule itself isn't needed. The repo works with
public financial statements, and naming the companies they belong to causes no harm.

## Decision

- The rule is removed from AGENTS.md, `.claude/rules/prompts.md`,
  `.claude/rules/adapters.md` and the code-reviewer's checklist.
- No name check is added. #76 is closed.

## Consequences

- Docs, tests, commits and PR text may name companies.
- Test data stays synthetic, as `.claude/rules/tests.md` asks. Tests shouldn't depend on
  real filings, and that rule is about hermetic tests, not names.
- The tickers files stay out of git because they live under `data/`, which is
  gitignored as a whole. ADR 0015 records the earlier reason for keeping them out.
- Prompts may name companies, but a prompt naming one would tune the agent to
  specific benchmark questions. That's an evaluation concern, judged per prompt change.
