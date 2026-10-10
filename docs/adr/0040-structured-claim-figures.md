# 0040: Claims can state their figure as data

## Context

The numeric verifier (ADR 0034) reads a claim's concept, period and figure from its
text. On real EDGAR data that rarely works. Concepts are long XBRL names claims don't
spell out, and many claims state several figures at once. In the first run with
verified claims only 1 of 32 could be checked (#144). Mapping common names onto
concepts was tried and dropped (#146). Each review round found phrasings that gave a
wrong CONTRADICTED, and it raised the checkable count only to 2 of 30. The owner chose
structured figures instead (#148).

## Decision

- `Claim` gets an optional `figure`: its `kind` (level, growth or ratio), its `value` as
  the claim writes it, and its `scale`. Nothing else comes from the model.
- The concept and the periods come from the rows the claim cites. A level cites one
  row, or versions of one row. Growth cites one concept in two periods. A ratio cites
  its numerator row, then its denominator row, in one period. A figure whose rows don't
  fit its kind is UNVERIFIABLE, with the reason.
- The figure becomes the verifier's `Reading`, and the existing check runs on it. So
  rounding to the stated decimals, STALE for a restated row and CONTRADICTED are the
  same as for text claims.
- The claim's text must state the figure's value and no other number. Periods such
  as FY2024 or Q3 don't count. Otherwise the claim is UNVERIFIABLE, so it can't pass
  on a figure its text doesn't say. This is the one thing still read from the text.
- A figure is checked only against the rows it cites. A figure that cites nothing is
  UNSUPPORTED, unlike a text claim, which is then checked against every row. So is a
  figure citing a ref that is neither a row nor a known non-fact source.
- Figures the text reader also refuses are UNVERIFIABLE: growth in a percentage
  concept (points or relative?), growth from a quarter to a full year, and a ratio of
  rows in different units or of percentages.
- The text's cause and outlook rules still apply. A claim that gives a reason isn't
  passed on its number.
- A malformed figure from the model, including a `value` that isn't a plain number,
  is dropped with a warning, and the claim is read from its text. A bad figure mustn't lose the answer.
- `VERIFIER_RULES` becomes "2".

## Consequences

- A figure can't be misread. What remains is the model citing the wrong row or stating
  the wrong number, which is what the verifier should catch.
- Nothing changes for the agent until its answer schema and prompts ask for figures.
  That is a separate, paid step, since the prompts are pinned by the baseline.
- Text claims are read exactly as before, from their rows in evidence order, so the
  labelled claim set is unaffected.
- A ratio stated as a multiple (a turnover of 6.49 times) matches the quotient, not the
  percentage. It is UNVERIFIABLE, not CONTRADICTED, since the gate fails on a
  CONTRADICTED claim (#145).
- A ratio is the first cited row over the second. A correct ratio cited in the wrong
  order is CONTRADICTED. The prompts in the next step must say which row comes first.
