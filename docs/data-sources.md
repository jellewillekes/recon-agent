# Data sources

## finance-agent-bench

- **Source**: [`vals-ai/finance-agent`](https://github.com/vals-ai/finance-agent), file `data/public.csv`
- **Pinned commit**: `8ba65f81ab759a8e0d44e72aabc5a47cf839d563` (`main`, 2026-07-21). The
  adapter fetches this exact commit, not `main`, so the schema can't shift under it.
  Bump the pin deliberately, in its own PR, if the upstream file changes.
- **License**: MIT. Copyright (c) 2025 Vals AI, Inc.
- **Required attribution**: `Data from vals-ai/finance-agent (MIT License), Copyright
  (c) 2025 Vals AI, Inc. https://github.com/vals-ai/finance-agent — benchmark details
  at https://www.vals.ai/benchmarks/finance_agent`
- **Size**: 50 rows, all fields populated, no duplicate questions.

### Actual schema (differs from the plan's assumption)

`docs/implementation-plan.md` assumed a validation set with an
`expected_tool_path`. The real file has none — it is pure question/answer pairs with
a per-question grading rubric, evaluated on the vals.ai platform by an LLM judge
against that rubric, not by comparing tool-call sequences:

| Column | Meaning |
|---|---|
| `Question` | Free-text financial research question. Company names, tickers, and periods are embedded in the text, not given as structured fields. |
| `Answer` | Reference answer. |
| `Question Type` | One of 9 categories (e.g. `Simple retrieval - Quantitative`, `Beat or Miss`, `Market Analysis`). |
| `Expert time (mins)` | How long a human expert took, numeric. |
| `Rubric` | A JSON array of `{"operator": "correctness" \| "contradiction", "criteria": "..."}` assertions specific to *this* question — not to be confused with our own dimension-level rubrics in `config/rubrics/`. |

### Mapping to `Case` (`docs/contracts.md` §1)

| `Case` field | From | Note |
|---|---|---|
| `case_id` | `sha256(Question)[:12]`, prefixed `finance-agent-bench:` | The source has no native ID. Content-derived so it's stable across re-fetches as long as the question text doesn't change, and doesn't depend on row order. |
| `source` | literal `"finance-agent-bench"` | |
| `question` | `Question` | |
| `expected_answer` | `Answer` | Always populated for this dataset. |
| `expected_tool_path` | `None`, always | See finding below. |
| `context` | `{"question_type", "expert_time_minutes", "rubric"}` | `rubric` is the parsed JSON list, carried through for the harness to use as case-specific grounding criteria later (step 5) — distinct from `config/rubrics/*.yaml`. |
| `tags` | `[Question Type]` | Single-element list; the source has one category per question. |
| `license` | `"MIT"` | |
| `attribution` | the string above | |

### Finding: this dataset never populates `expected_tool_path`

Every `Case` from this adapter has `expected_tool_path = None`. That's valid per the
`Case` contract (each case still has `expected_answer`), but it means the
`tool_path_exact` and `tool_path_equivalent` metrics in `CaseScore` (`docs/contracts.md`
§7) have nothing to compare against for this source — they'll trivially score as
"not applicable" for every case from `finance-agent-bench`. `tool_call_accuracy` is
still meaningful (it doesn't require a reference path). Flagged for step 5: the
harness needs to treat "no reference path" as distinct from "wrong path", not
silently score it as a failure.

### Also worth knowing for step 3

The original finance-agent benchmark answers these questions using `web_search`,
`edgar_search`, `parse_html_page`, and `retrieve_information` against live sources —
the dataset itself carries no structured `context` (no source documents, no
ticker/period fields) to query locally. Step 3's own tool set has to be decided
against what's actually available to us (e.g. local SEC filing data, DuckDB tables we
populate ourselves), not assumed from this dataset's `context`, which is sparse by
design.
