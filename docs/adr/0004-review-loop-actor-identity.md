# ADR 0004: Trust github-actions[bot] alongside claude[bot] in the review loop

Status: Accepted
Date: 2026-09-07

## Context

PR #32 fixed the responder's trigger condition and confirmed it live on PR #31: the
responder executed for the first time ever, fixed three bugs, and pushed a commit. But the
resulting round-3 re-review failed outright: `"Workflow initiated by non-human actor:
github-actions (type: Bot). Add bot to allowed_bots list or use '*' to allow all bots."`

Fetching `anthropics/claude-code-action`'s `action.yml` directly clarified why: commit
authorship (`bot_id`/`bot_name`, defaulting to `claude[bot]`) and the git push's
authenticating identity are two separate things. `claude-respond-to-review.yml`'s
`actions/checkout` step never overrides `token:`, so the push authenticates with the
default `GITHUB_TOKEN` — actor `github-actions[bot]` — even though the commit itself is
correctly authored as `claude[bot]`. `claude-code-review.yml`'s `allowed_bots: "claude[bot]"`
only listed the commit-author identity, not the push-actor identity, so the resulting
`synchronize` event was rejected.

Getting the push to authenticate as `claude[bot]` instead would need a GitHub App
installation token minted before `actions/checkout` runs; the action only exposes its own
app token as an output of the step that runs *after* checkout, and no separate App
ID/private-key secret exists to mint one independently.

## Decision

Add `github-actions[bot]` to `claude-code-review.yml`'s `allowed_bots`, alongside
`claude[bot]`: `"claude[bot],github-actions[bot]"`.

This does widen a security-relevant allowlist — any `github-actions[bot]`-attributed push
to a PR branch can now auto-trigger a review, not just ones from our own responder. In this
repo specifically that's a narrow widening in practice: no other workflow pushes commits to
PR branches, and dependabot's pushes carry `dependabot[bot]`'s own identity, not
`github-actions[bot]`.

## Consequences

The loop's re-review step no longer rejects the responder's own pushes. If a future
workflow in this repo starts pushing commits to PR branches using the default
`GITHUB_TOKEN`, its pushes would also be able to trigger an automatic re-review under this
same allowlist entry — worth remembering if that changes the "narrow in practice" reasoning
above.

A separate, still-open finding from the same live test: every workflow run on that push
also landed in GitHub's `action_required` hold, needing manual approval before running at
all. No repo-level API setting was found that explains or controls this, and it's not
addressed by this ADR — to be observed on the next live test.
