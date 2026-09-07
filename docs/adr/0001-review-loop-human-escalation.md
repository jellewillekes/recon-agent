# ADR 0001: Fix the dead review-respond trigger, add human-decision escalation

Status: Accepted
Date: 2026-09-07

## Context

`docs/github-agents.md` documents a review loop: Reviewer (`claude-code-review.yml`)
requests changes → Responder (`claude-respond-to-review.yml`) fixes what it agrees with and
pushes a commit → Reviewer re-reviews → repeat up to 3 rounds → unresolved after round 3
hands off to a human. In practice, the Responder had never fired on any PR in this repo's
history. Its trigger guard checked `github.event.pull_request.user.login` (who opened the
PR) instead of `github.event.review.user.login` (who submitted the review) — every PR here
was opened by a human-attributed local session, never the bot, so the guard never matched.

Separately, neither the Reviewer's nor the Responder's prompt distinguished an ordinary
correctness/design finding from a concern that is not the loop's to resolve at all —
anything in CLAUDE.md's "Forbidden without explicit permission" list (golden-set content,
evaluation thresholds, new dependencies, repo layout, secrets), or a point a PR description
already flagged as a draft. Nothing stopped the Responder from, say, editing rubric content
to satisfy a reviewer comment.

## Decision

- Fix the trigger condition to check the reviewer's identity, not the PR author's.
- Add an explicit rule to both prompts: a Forbidden-list item, or a PR-description-flagged
  draft, gets called out as a distinct "Needs a human decision" item and never drives
  `REQUEST_CHANGES` or an automatic commit on its own, regardless of round number.
- Round cap stays at 3 — confirmed correct, not changed.

## Consequences

The auto-fix loop now actually runs. Verified live on PR #31: round 2's review correctly
separated three ordinary blocking bugs from a distinct "Needs a human decision" line
covering two PR-description-flagged items, and the Responder — executing for the first
time ever in this repo — fixed the three ordinary bugs, added tests for each, and correctly
left the human-decision items and a fourth (architecturally harder) bug untouched. Shipped
as PR #32.

Two follow-on gaps surfaced by that same live run, not yet fixed:

- The Responder never posted its required summary comment (57 turns, 14 permission
  denials, no `mcp__github__create_issue_comment` call landed) — plausibly because its
  `allowedTools` (`Bash(git:*)` only) gives it no way to run `uv run pytest`/`ruff` to
  verify its own fix before committing, and it may have spent its budget retrying that.
- The commit it pushed is attributed to `github-actions[bot]`, not `claude[bot]` —
  `claude-respond-to-review.yml`'s checkout step doesn't override the default
  `GITHUB_TOKEN`, so the resulting `synchronize` event's actor doesn't match
  `claude-code-review.yml`'s `allowed_bots: "claude[bot]"`, and the re-review fails
  outright ("Workflow initiated by non-human actor: github-actions"). This also puts every
  `pull_request`-triggered workflow on that push into GitHub's `action_required` hold,
  needing manual approval (`gh api -X POST .../actions/runs/<id>/approve`) before anything
  runs at all.

Both are follow-up work, not fixed here.
