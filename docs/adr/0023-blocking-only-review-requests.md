# ADR 0023: Only blocking defects request changes

Status: Accepted
Date: 2026-10-01

## Context

The reviewer prompt allowed unresolved design concerns to trigger
`REQUEST_CHANGES`. To measure the resulting churn, I reviewed the 20 most
recently merged PRs as of this date and classified each inline finding attached
to a `CHANGES_REQUESTED` review. The sample contained 9 such reviews across
PRs #86, #85, #80, #63, #57, and #47, with 20 inline findings.
The 20-PR sample, in merge order, was #88, #86, #85, #84, #83, #82, #81, #80,
#69, #68, #50, #65, #63, #67, #57, #56, #55, #49, #48, and #47.

Classification uses the finding's substance, not its `Blocking`/`Question`/
`Note` tag. A defect names behavior that is wrong for a concrete input or state.
A design preference recommends a different approach without identifying wrong
behavior. A hypothetical describes an unobserved future state. Repeated
comments are counted as findings because each appeared in a separate review.

| Classification | Findings | Examples |
| --- | ---: | --- |
| Real defects | 10 | Empty eval selection reported success; push guard missed `git push origin HEAD` on `main`; worker limits were not enforced. |
| Design preferences | 5 | Graceful handling for malformed config; missing direct CLI tests; an unused constant; a metric's intended normalization behavior. |
| Hypotheticals | 5 | Future test coupling to editable thresholds; unsupported fiscal-calendar periods; a later resume across event loops. |

Six of the nine change-request rounds contained at least one real defect. Three
rounds did not: both rounds on PR #86 and the review on PR #57. Five of the 10
defect findings were tagged `Question:` or `Note:` rather than `Blocking:`. The
old prompt therefore had both problems: it let non-defects start rounds, and it
did not clearly tell the reviewer to promote a concrete defect raised as a
question into a blocking finding.

## Decision

Only an unresolved `Blocking:` finding can trigger `REQUEST_CHANGES`. The
reviewer must name a concrete input or state and explain the incorrect behavior
or violated requirement. Questions and notes remain welcome, including design
discussion, but they use `COMMENT` and do not start another round. If a question
reveals a concrete defect, the reviewer rewrites it as a `Blocking:` finding
with the case that reproduces it. Human-decision items remain non-blocking, and
the three-round cap is unchanged.

## Consequences

The 20-PR sample is the baseline for the next 10 merged PRs: 0.45
`REQUEST_CHANGES` rounds per merged PR (9 / 20), with a concrete defect in 6 of
9 rounds. For the follow-up, count real defect findings in each
`REQUEST_CHANGES` round using the same classification rules. Count a finding
again when it reappears in a later round. Compare the average number of rounds
per PR and defect findings per round. The post-change measurement remains open
until 10 PRs have merged.
