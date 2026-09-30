# ADR 0017: Claude Code project setup — enforce hard rules, load the rest on demand

Status: Accepted
Date: 2026-09-29

## Context

Issues #70–#73 and #75, from a review of the agentic-framework repo. Every rule in
CLAUDE.md was prose the agent had to remember, including the expensive ones: never run
a full evaluation, never push to `main`, never skip pre-commit. CLAUDE.md had grown to
196 lines, loaded every turn, much of it relevant to only some files. The framework's
principle is that instructions are advisory and hooks and deny lists are enforced,
which Claude Code's own memory docs repeat.

Four choices had more than one reasonable option.

- **Adopt the framework as-is, or adapt it.** Its plugin can be enabled from
  `.claude/settings.json`, and its overlay template can be rendered with Copier. The
  plugin brings six skills we don't use, a guard aimed at cloud commands, and a format
  hook that looks for `ruff` on `PATH`, where ours lives in the venv. It would also be
  fetched from GitHub at the start of every CI bot run. The overlay ships GCP, SQL and
  notebook rules that don't apply here and would conflict on every `copier update`.
- **Block or ask before evaluations.** Every `recon.cli eval` and `run` spends credit.
- **Stop gate cost.** Running `make test` before a turn may end costs about 40 s.
- **One instruction file or two.** Other coding agents read `AGENTS.md`, and Claude
  Code reads only `CLAUDE.md` when both exist.

## Decision

Adapt the framework's pieces locally, crediting the source in each file.

- `AGENTS.md` holds the conventions for any agent, with the framework's nine behaviour
  rules between `rules:start`/`rules:end` markers. `CLAUDE.md` is `@AGENTS.md` plus
  Claude-only lines, including what to keep when compacting. The Voice examples move to
  `docs/voice-examples.md`. The Voice rules stay loaded, because the review bots can't
  read other files.
- `.claude/rules/` holds five path-scoped rule files (tests, prompts and model config,
  adapters, contracts, the eval harness) that load only when Claude reads a matching file.
- `.claude/settings.json` pre-approves the check commands, **asks** before any
  `recon.cli eval`, `run` or `edgar fetch`, and denies reading `.env`.
- A PreToolUse guard **blocks**:
  - `recon.cli eval` without `--limit`
  - any push that targets `main`: `main`, `HEAD:main`, `+main`, `refs/heads/main`, or
    a bare push or `git push origin HEAD` while on `main`. `git -C <dir>` is judged by
    that repo's branch
  - force-push without `--force-with-lease`, and `--no-verify`
  - `DROP` or `TRUNCATE` (with or without `TABLE`) in a pipeline that runs a SQL client
  - shell reads of `.env` files

  It parses commands into shell tokens, so text inside a quoted commit message or PR
  body doesn't trigger it. A regex version could backtrack exponentially, which CodeQL
  flagged. Permission rules match the command text Claude usually writes and aren't a
  security boundary, so the hook catches the variants they miss. `RECON_ALLOW_GUARDED=1`
  in Claude Code's own environment overrides it.
- A Stop hook runs `make test` when Python files changed and blocks the stop once per
  prompt. If tests still fail at the next stop, it lets Claude finish and shows the
  user a warning (`systemMessage`). It doesn't hand Claude a note through
  `additionalContext`. A live run showed that a Stop hook's `additionalContext` starts
  another turn, which looped until `max_turns`. It is off in GitHub Actions, where the
  bots have no synced venv, and with `RECON_STOP_GATE=0`.
- A PostToolUse hook runs `ruff format` after each Python edit. It doesn't run
  `ruff check --fix`, which deletes an import added one edit before its first use.
- Two read-only subagents: `code-reviewer` (correctness and scope only, a finding
  needs a concrete failing input) and `eval-reviewer` (leakage, run comparability,
  noise, graders, cost).

## Consequences

- Turns that change Python end about 40 s later. `RECON_STOP_GATE=0` turns it off for
  a session.
- A full evaluation now needs the user to run it or to set `RECON_ALLOW_GUARDED=1`.
- One accepted false positive: SQL text inside a pipeline that runs a SQL client is
  scanned as a whole, so `psql -c "SELECT 'truncate the log'"` is blocked. Telling a
  string literal from a statement would mean parsing SQL. For a guard, a rare false
  block is the safer error.
- The CI bots use `main`'s setup, never a PR's. `claude-code-action` restores `.claude/`,
  `CLAUDE.md` and related files from `origin/main` before running, because a PR head is
  untrusted, and moves the PR's copies to `.claude-pr/` unexecuted. So this setup
  reaches the bots only once merged. From then on the guard applies to them and the Stop
  gate skips itself. The five review rounds on the PR that added it ran without hooks.
- For the same reason the review responder can't fix a PR's `.claude/` files. It says
  so in its summary, and a person or a local session applies the fix.
- `AGENTS.md` isn't on the action's restore list, and `CLAUDE.md` imports it. So on a PR,
  the bots read the PR's own `AGENTS.md`, and a PR (including one the implement bot
  opens) can change the rules its reviewer follows. Restoring `AGENTS.md` from `main`
  in the workflows closes this. It's a workflow change, so it belongs in its own PR.
  #81 did this for the review and respond workflows. `claude.yml` can't restore the
  checkout, because the action checks out the PR branch itself after any workflow
  step, so it passes the default branch's `AGENTS.md` as `--append-system-prompt-file`
  instead. That outranks the PR's copy but doesn't remove it.
- `claude-code-action` skips its run when a PR changes the workflow it runs from. Keep
  workflow edits out of PRs that need a bot review.
- Project `allow` rules take effect only after the workspace is trusted, by opening
  Claude Code here interactively once. Hooks, `ask` and `deny` rules apply before that.
- On Claude Code 2.1.284 the Stop event's `stop_hook_active` is false on a prompt's first
  stop and true after a block. The gate keys on `session_id` and `prompt_id` instead, and
  uses the flag only as a fallback.
- The agents this harness evaluates are unaffected. Their sessions pass
  `setting_sources=[]` (ADR 0016), so they never load `.claude/`, and scores stay
  comparable.
- `tests/test_claude_setup.py` runs the hooks on JSON events with no model, and fails if
  a rule's `paths` stop matching a file or a subagent gains a write tool.
- The combined always-loaded instructions went from 196 to 180 lines. The rest of the
  saving is in what now loads only on demand.
