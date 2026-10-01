# ADR 0024: The review bot requests changes only for Blocking findings

Status: Accepted
Date: 2026-10-01

## Context

Issue #74. The review prompt asked the bot to "weigh in on design and best-practice
tradeoffs", aiming for "a genuine back-and-forth", and to submit `REQUEST_CHANGES` for
any unresolved correctness or design concern. Each `REQUEST_CHANGES` starts the
responder and another review round, which costs Claude usage and the user's time.

Measured on the merged PRs up to #86: 27 PRs had bot reviews, 75 rounds in total, and
29 rounds requested changes. Since #40, findings carry severity tags. Of the 21
change-requesting reviews since then:

- 14 had at least one `Blocking:` finding. Each Blocking finding named a concrete
  failure: config left out of a hash, an environment variable not passed to a
  subprocess, a run budget bypassed, gaps in the guard hook.
- 7, a third, had only `Question:` and `Note:` findings: #44, #45, #46, #57, #85 and
  #86 twice. Each still cost a round.

Reviews before #40 had no inline tags, so they weren't classified.

### Substance classification on the latest 20 merged PRs

I also classified the substance of every inline finding attached to a
`CHANGES_REQUESTED` review in the 20 most recently merged PRs as of 2026-10-01.
This is a newer sample than the tag-based count above. The sample, in merge
order, was #88, #86, #85, #84, #83, #82, #81, #80, #69, #68, #50, #65, #63,
#67, #57, #56, #55, #49, #48, and #47. It contained 9 change-request reviews
across #86, #85, #80, #63, #57, and #47, with 20 inline findings.

A real defect describes behavior that is wrong for a concrete input or state.
A design preference recommends another approach without identifying wrong
behavior. A hypothetical describes an unobserved future state. Findings are
classified by their substance, not their severity tag. Repeated comments count
again when they recur in another round.

| Classification | Findings | Examples |
| --- | ---: | --- |
| Real defects | 10 | Empty eval selection reported success; push guard missed `git push origin HEAD` on `main`; worker limits were not enforced. |
| Design preferences | 5 | Graceful handling for malformed config; missing direct CLI tests; an unused constant; a metric's intended normalization behavior. |
| Hypotheticals | 5 | Future test coupling to editable thresholds; unsupported fiscal-calendar periods; a later resume across event loops. |

Six of the nine rounds contained at least one real defect. Five of the 10
defect findings had been tagged `Question:` or `Note:`. The prompt should tell
the reviewer to restate such a finding as `Blocking:` when it can name the
concrete failing case.

## Decision

- The bot tags a finding `Blocking:` only when it can name a concrete input or state
  that goes wrong: a bug, a security hole, data loss, or a stated requirement the PR
  misses.
- `REQUEST_CHANGES` only with at least one unresolved Blocking finding. Questions,
  notes and design discussion go out as `COMMENT`, which doesn't start the responder.
  The PR's author answers them.
- Design discussion stays welcome, as non-blocking findings. The three-round cap and
  the "Needs a human decision" handling from ADR 0001 are unchanged.

## Consequences

- About a third fewer rounds, judging by the past reviews, without losing a Blocking
  finding.
- Answering a question is now the author's job, not the responder's. A question that
  goes unanswered doesn't block a merge.
- Check the effect over the next 10 PRs: rounds per PR, and how many of the
  `REQUEST_CHANGES` rounds carried a Blocking finding.
