---
paths:
  - ".github/workflows/**/*"
---

# GitHub workflows

- `claude-code-action` skips its run on a PR that changes the workflow it runs from, so such a PR gets no bot review. Keep workflow edits in their own small PR.
- The action restores `.claude/`, `CLAUDE.md` and a few other files from the default branch before running, and never runs a PR's copies. It does not restore `AGENTS.md`. The review and respond workflows do that in an inline step, and `claude.yml` passes the default branch's copy with `--append-system-prompt-file`. See `docs/github-agents.md`.
- Keep that restore step inline and identical in both workflows; `tests/test_workflows.py` fails if they drift. A script would be read from the PR's untrusted checkout.
- Pin every action to a full commit SHA with the version in a comment, like the existing steps.
- Pass `github.*` values into `run:` through `env:`, never as `${{ }}` inside the script; zizmor flags template injection.
- The bots have no model credentials beyond `CLAUDE_CODE_OAUTH_TOKEN` and never run `recon.cli eval`. Don't add a workflow that calls a model outside `claude-code-action`.
