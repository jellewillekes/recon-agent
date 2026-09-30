# Contributing

Keep changes small, test-backed, and scoped to one plan step per PR. See `AGENTS.md`
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

## Working with Claude Code

The repo ships a Claude Code setup in `.claude/` (`docs/adr/0017-claude-code-project-setup.md`).
`CLAUDE.md` imports `AGENTS.md`, so both Claude Code and other coding agents read the same
conventions.

- Open Claude Code in the repo interactively once and accept the trust prompt. Until you
  do, the shared `allow` list (e.g. `make check` without a prompt) is ignored. The hooks
  and the `ask`/`deny` rules apply either way.
- A guard hook blocks `recon.cli eval` without `--limit`, pushes to `main`, force-push,
  `--no-verify`, `DROP`/`TRUNCATE` sent to a SQL client, and shell reads of `.env`. To run
  one on purpose, run it yourself, or start Claude Code with `RECON_ALLOW_GUARDED=1`.
- A Stop hook runs `make test` before a turn ends when Python files changed, which adds
  about 40 s. Start Claude Code with `RECON_STOP_GATE=0` to turn it off for a session.
- Rules for specific paths are in `.claude/rules/`. Two read-only review subagents,
  `code-reviewer` and `eval-reviewer`, are in `.claude/agents/`.
- Personal approvals go to `.claude/settings.local.json` and personal notes to
  `CLAUDE.local.md`. Both are gitignored.
- The review bots always run with `main`'s `.claude/` and `CLAUDE.md`, never a PR's, so
  they can't test or fix a PR's changes there. A person or a local session does that.
  The review and respond workflows also use `main`'s `AGENTS.md`. `@claude` runs on a
  PR (`claude.yml`) get `main`'s copy as system prompt but still see the PR's own, so
  look over `AGENTS.md` changes before asking `@claude` to act on that PR.

## Guardrails

- Don't bypass pre-commit with `--no-verify` — fix the underlying issue
- Don't `git push --force` to `main`
- Never fill in, generate, or modify the golden set or expected answers — see
  `AGENTS.md`
- Never lower an evaluation threshold because a test fails
- Generated or ignored paths (`.venv/`, `__pycache__/`, `data/*` except its
  `README.md`) are not committed — `scripts/precommit_block_forbidden_tracked_paths.sh`
  enforces this
