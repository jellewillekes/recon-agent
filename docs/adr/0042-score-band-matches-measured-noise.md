# 0042: The answer score band matches the measured noise

## Context

The gate treats a drop of up to 0.10 in the answer score as noise (ADR 0028). That value
was a judgement from two runs of the 7 text cases. The baseline now runs on 6 numeric
cases (ADR 0041), and three runs of the same agent there give a measured band:
`estimate_noise` puts run-to-run noise at ±0.117 at 95% (3 runs, 6 cases). That is wider
than 0.10, so `compare` warned that an unchanged agent could fail the gate on noise alone,
as a text-case rerun did in #144. The means of the three runs moved by only 0.012. Per-case
scores swing more, by up to 0.30.

## Decision

- `answer_score_noise_band` becomes 0.12, the user's decision on 2026-10-11. It covers the
  measured ±0.117.
- The band stays a fixed value in `config/thresholds.yaml`, not one the gate derives from
  the data (ADR 0036).

## Consequences

- An unchanged agent is less likely to fail the gate on noise, and `compare` stops warning
  about the band for the baseline's settings. A 95% band still lets about one rerun in 20
  fall outside it.
- The margin is small: 0.12 against a measured 0.117. One more stored repeat with a little
  more spread per case would bring the `compare` warning back.
- A real drop in the answer score smaller than 0.12 passes the gate unnoticed.
- The band rests on three runs. More repeats, or more cases, would narrow the estimate.
