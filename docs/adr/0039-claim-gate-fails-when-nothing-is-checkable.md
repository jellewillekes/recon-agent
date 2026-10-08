# 0039: The claim gate fails a candidate it can't check

## Context

The gate's claim rule (ADR 0035) compares the share of unsupported and contradicted
claims among those the verifier could read. When it can read none of a candidate's
claims, that share is None and the rule did nothing. A change that turns every claim
into prose the verifier can't read, or stops the agent citing fact rows, would pass the
claim check with nothing checked. The #143 review found this, with two related gaps:
the gate skipped the check against an unverified baseline without saying so, and a
fact result cut at 500 rows could keep different restated rows when replayed. The
choice was between failing such a candidate and only warning.

## Decision

- The gate fails a candidate when the verifier could read none of its claims and could
  read some of the baseline's. It doesn't fail when neither side had a readable claim.
- When the baseline has no verified claims, `eval` and `compare` print that the claim
  check didn't run. The rules themselves don't change for that case.
- `get_financial_fact` orders tied rows by `filed` and `accession`, so a truncated result
  is the same on replay.

## Consequences

- A candidate can now fail the claim rule without a single bad claim. The message names
  the cause and points to the UNVERIFIABLE reasons in the run's summary.
- On cases where few claims are readable, such as the text cases (`docs/eval-noise.md`),
  one run that happens to make no readable claim fails the gate. That is the price of
  not passing what wasn't checked.
