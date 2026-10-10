# 0034: Numeric claims are verified by recomputation, against labelled claims

## Context

ADR 0030 checks that a cited ref matches a row a tool returned. It doesn't check that the
row supports the claim. A claim can cite a real row and still state the wrong figure, the
wrong period or a value a later filing replaced. Many financial claims are numeric, so
they can be recomputed exactly, with no model call and no credit (#137).

A verifier is only worth its verdicts if it is measured against claims whose right verdict
is known. That needs a labelled set before the verifier, and a rule for every place two
readings of a claim are possible.

## Decision

`recon.eval.claim_verifier.verify_claim(claim_id, text, rows)` returns a `ClaimVerification`.
Its input is the claim text and the tool rows it rests on, not a database, so it runs
without data and on rows replayed from a stored run.

Verdicts:

- `SUPPORTED`: the recomputed figure matches the claim.
- `CONTRADICTED`: the rows give a different figure, or the claim's figure belongs to
  another period.
- `STALE`: the claim matches the first-filed value, and a later filing replaced it.
- `UNSUPPORTED`: no row for a period or concept the claim needs.
- `UNVERIFIABLE`: the verifier can't read the claim, or can't tell which row is current.
- `PARTIALLY_SUPPORTED`: part of a claim holds and part doesn't. The numeric verifier
  never returns it. It is the label for a figure that holds with an unsupported cause,
  and it is the target of a later model-based verifier.

Rules:

- Three kinds of claim are read: a level, a growth rate between two periods (one period
  means against the prior year) and a ratio of two concepts. Anything else is
  `UNVERIFIABLE`. A claim that also states a cause, a qualifier or an outlook is
  `UNVERIFIABLE` too, because its number alone doesn't settle it. So is a negated claim:
  "not $480 million" is true when the value is 450, and must not read as a claim of 480.
- A figure matches when it equals the recomputed value rounded to the precision the claim
  states. "8.3%" allows 0.05 points and "$450 million" allows half a million. An optional
  `tolerance` adds to that.
- When several rows describe one period, the latest `filed` is current and the earliest is
  the first-filed value. Several values with no filing dates are `UNVERIFIABLE`.
- A level that is missing for its own period but equals another period's value is
  `CONTRADICTED` (wrong period). With no such match it is `UNSUPPORTED`.
- Rows in a unit other than `USD` or `USD_M`, or a percentage concept with a change
  ("rose 1 point to 42%"), are `UNVERIFIABLE`. Since #145, a row in unit `pure`
  (EDGAR's fraction, 0.109) is read as a percentage (10.9%). A change in such a rate is
  then UNVERIFIABLE like any percentage concept. Before, "fell 4.1%" for a rate from 15%
  to 10.9% was read as relative change and came back CONTRADICTED though it is 4.1 points.

The labelled set is `evals/verification-claims.yaml`: claims with inline rows, a scope
(`numeric` or `llm`) and the expected verdict. Claims in the `llm` scope must come back
`UNVERIFIABLE` from the numeric verifier, so it can't over-claim. The test suite checks
every `numeric` claim against its label and prints precision and recall per verdict.

The report and endpoint (`VerificationReport`, `POST /verify`) and the gate check are the
next PR, and the models move into `contracts.py` there, as AGENTS.md asks for contract
changes.

## Consequences

- 100% agreement on the set is weak evidence. The set and the verifier were written
  together, so it shows the rules are consistent and not that they generalise. Claims
  from real agent answers should be added and labelled by the owner.
- The labels are drafts written by Claude from synthetic rows. AGENTS.md says labels come
  from the user, so the file is marked draft until the owner reviews each verdict.
- The EDGAR tables keep one row per period and the latest filing wins (ADR 0015). `STALE`
  therefore only arises when rows from different filings or snapshots are supplied, such
  as a claim written against an older snapshot.
- The parser reads a documented set of phrasings. A phrasing it misses is `UNVERIFIABLE`,
  which is safe and shows up as lower recall on the `numeric` scope.
- Causes, outlook and qualitative statements need a model-based verifier and a paid
  calibration run against human labels. That is a separate issue.
