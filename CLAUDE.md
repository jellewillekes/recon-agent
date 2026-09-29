@AGENTS.md

## Claude Code

- Rules for specific paths live in `.claude/rules/` and load only when you read a matching file.
- `.claude/settings.json` blocks `recon.cli eval` without `--limit`, pushes to `main`, force-pushes, `--no-verify` and `.env` reads, and asks before any `eval`, `run` or `edgar fetch`. If the guard blocks you, explain what you wanted to run and let the user run it.
- A Stop hook runs `make test` when Python files changed. If it blocks you, fix the cause; don't weaken or skip a test.
- Before opening a PR, use the `code-reviewer` subagent on the diff. For eval results, prompt, rubric or gate changes, also use `eval-reviewer`.
- For Claude Code or Agent SDK behaviour, fetch https://code.claude.com/docs/llms.txt and the linked page instead of relying on memory. ADR 0016 exists because the SDK behaved differently than assumed.
- When compacting, keep: the list of modified files, the last `make check` result, open PR and issue numbers, failing tests with the commands that reproduce them, and open questions for the user.
