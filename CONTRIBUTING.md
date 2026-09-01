# Contributing

Keep changes small, test-backed, and scoped to one plan step per PR. See `CLAUDE.md`
for the full set of project conventions — this file is the PR-facing checklist.

## Before opening a PR

Match verification to the scope of the change:

| Change scope | Run |
| --- | --- |
| logic in `src/` | `make check` |
| a step's own verification command | run it, paste the output in the PR |
| anything touching a contract | re-run `uv run pytest tests/test_contracts.py -q` |

Do not claim a check was run if it was not run.

## PR expectations

- Branch from `main`
- One plan step per PR — do not combine two steps
- Keep the PR focused
- Use squash merge

## PR title

PR titles must use Conventional Commits: `feat:`, `fix:`, `docs:`, `refactor:`,
`test:`, `ci:`, `chore:`, `deps:`.

## Local workflows

- `make check` — format check, lint, typecheck, tests (excluding `llm`-marked)
- `make fix` — format + safe autofix
- `make precommit` — run all pre-commit hooks
- `make install-hooks` — install git hooks locally

## Guardrails

- Don't bypass pre-commit with `--no-verify` — fix the underlying issue
- Don't `git push --force` to `main`
- Never fill in, generate, or modify the golden set or expected answers — see
  `CLAUDE.md`
- Never lower an evaluation threshold because a test fails
- Generated or ignored paths (`.venv/`, `__pycache__/`, `data/*` except its
  `README.md`) are not committed — `scripts/precommit_block_forbidden_tracked_paths.sh`
  enforces this
