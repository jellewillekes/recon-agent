# ADR 0015: SEC EDGAR tool data — company list, cutoff, and fact derivation

Status: Accepted
Date: 2026-09-28

## Context

Issue #58, part A of #23. The MCP tools answer from a synthetic fixture, so answers to
finance-agent-bench's questions about real companies stay near zero. SEC EDGAR's
`companyfacts` API has structured XBRL facts for every US filer. Four things about it
are not obvious and shape the design.

- `fy`/`fp` on a fact describe the filing that reported it, not the period it covers.
  A fiscal-2024 10-K carries fiscal 2022 and 2023 comparatives, all tagged `fy=2024`.
  Filtering on `fp == "FY"` and deduplicating by `fy` silently collapses comparative
  years.
- The same period often appears in several filings, and a later filing may restate it.
- Quarterly filers never tag a fourth quarter in a 10-Q. The full year and sometimes
  Q4 itself appear in the 10-K, and 10-Qs carry six- and nine-month year-to-date
  facts.
- The API only has standard-taxonomy, non-dimensional facts. Company-specific KPIs,
  segment splits and share-class breakdowns aren't there at all.

Three decisions were settled with the user.

- **Company list.** CLAUDE.md forbids naming companies in committed files, so a
  checked-in ticker list is out. The list is derived at fetch time from the questions:
  explicit tickers, plus registered names from SEC's `company_tickers.json`. It is
  written to a gitignored file for review before anything else is fetched. The other
  option, a hand-maintained file with no derivation, was rejected as needless manual
  work.
- **Point-in-time cutoff.** Without one, the agent sees filings made after a question
  was asked. `filed_cutoff` in `config/sec_edgar.yaml` drops every fact and filing
  filed after it. It's set to the latest "as of" date in the dataset's questions. One
  global cutoff can't match every question's own date, so earlier-dated questions can
  still see somewhat later filings.
- **User-Agent.** SEC requires a contact User-Agent. It comes only from
  `SEC_EDGAR_USER_AGENT` and is never written to a file.

## Decision

`adapters/sec_edgar.py` fetches into `data/raw/sec_edgar/<snapshot>/`, where the
snapshot id is the fetch date. It stays under 10 requests/second, retries 429/5xx, and
records a sha256 per file in `manifest.json`. It also fetches the older submissions
pages, because frequent filers push their older 10-Ks and 10-Qs out of `recent`, and
their report dates are needed below.

`adapters/sec_edgar_normalize.py` writes four Parquet tables
(`companies`, `concepts`, `financial_facts`, `filings`) under
`data/processed/sec_edgar/<snapshot>/`. For `financial_facts`:

- One row per (company, taxonomy, concept, unit, start, end). Only entries filed on or
  before the cutoff count.
- The value, form, filed date and accession come from the **latest** such filing, so
  restatements win.
- `fiscal_year`/`fiscal_period` come from the filing in which the period was the
  **current** one, i.e. the fact's `end` equals that filing's `reportDate`. A fact
  that was never a current period (e.g. a cover-page share count dated after period
  end) gets neither.
- `fiscal_period` is derived from duration. A current-period fact in a 10-K is `FY`
  for about a year and `Q4` for about a quarter. A 10-Q quarter keeps its `Q1`–`Q3`.
  Year-to-date durations get `NULL`.
- Nothing is computed. There's no synthesized Q4 and no derived ratios, following the
  adapters' "map and validate only" rule. The agent derives Q4 as the full year minus
  the nine-month year-to-date itself.

## Consequences

- XBRL answers roughly 10–14 of the 50 questions. Guidance (Beat or Miss) lives in 8-K
  press-release text, and qualitative questions need document text. Both need step 13.
- Re-fetching on a later date gives a new snapshot. A result isn't reproducible unless
  it records which snapshot it queried, which is tracked in #62.
- Name matching is a heuristic. Abbreviated names and wrong tickers in question text
  need a manual fix in the tickers file. `edgar stats` lists cases with no match.
- `fiscal_period` is classified by duration in days (`_YEAR_DAYS`, `_QUARTER_DAYS` in
  `sec_edgar_normalize.py`), not by a fiscal calendar. A 52/53-week or 4-4-5 filer's
  regular quarters and years already fall inside these ranges (a 53-week year's extra
  week lands in one quarter, stretching it to ~98 days, still within `_QUARTER_DAYS`).
  An irregular period outside the ranges gets `NULL` rather than a guessed label,
  consistent with "nothing is computed" above. Accepted for the companies in scope;
  revisit the ranges if a real filer's period falls outside them.
- Part B (#59) points the tools at these tables. Tool names and the `ToolResult` shape
  don't change.
