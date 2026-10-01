# ADR 0025: Retrieval over SEC filing text

Status: Accepted
Date: 2026-10-01

## Context

Step 13 (#18) adds a retrieval tool. The plan's corpus, generic markdown in
`data/knowledge/`, wouldn't help the benchmark. About half its 50 questions need text
that XBRL facts don't carry: 9 qualitative, 7 beat-or-miss (guidance is in earnings
releases), 4 adjustments, 3 market analysis and 3 complex retrieval. The plan text gave
the tool to the supervisor and critic only, which would leave single mode, and so the
smoke-set baseline, without it.

## Decision

The user decided:

- **Corpus:** EX-99.1 exhibits of 8-Ks reporting results (item 2.02), plus the risk
  factors (1A), MD&A (7) and market risk (7A) sections of 10-Ks. Only filings in the two
  years before `filed_cutoff`, for the companies in the tickers files.
  `recon.cli edgar fetch-text` fetches them with the existing rate-limited SEC client.
- **Roles:** the single-mode investigator, `worker_facts` and the critic get
  `search_knowledge`. The supervisor only routes, and `worker_lookup` resolves ids.
  It's set in `config/roles.yaml` and the single-mode tool list, not in prompts.
  The grants land in part 2, not part 1. Today's prompts tell those roles there is
  no document text, so granting the tool needs prompt changes. A prompt change needs
  a new baseline (the prompt-baseline check), which part 2 makes anyway because the
  corpus changes `tool_data_snapshot`. Part 1 ships the tool with no role allowed to
  call it.
- **Models:** `sentence-transformers` for embeddings (`BAAI/bge-small-en-v1.5`) and the
  cross-encoder reranker (`cross-encoder/ms-marco-MiniLM-L-6-v2`), on CPU.
- **Keyword search:** Postgres full-text search rather than `rank_bm25`. It runs in the
  same database and holds nothing in memory. Reciprocal rank fusion merges it with
  dense search, and the cross-encoder reranks the merged top 30.

Built to fit the rest of the harness:

- Embeddings live in pgvector in the existing Postgres, per the storage boundary.
- The chunks are written to `knowledge_chunks.parquet` in the processed EDGAR snapshot.
  Their hash, with the model names, is the corpus id. It's appended to
  `tool_data_snapshot` as `-k<id>`, so the gate never compares runs across corpora.
  Runs without a corpus keep their old id.
- `search_knowledge` is registered only when a corpus exists. It returns `unavailable`
  when the index isn't reachable, isn't built, or the models aren't installed.
- `sentence-transformers` and `torch` are an optional `knowledge` extra. The API image
  runs on the fixture, which has no corpus, so it leaves them out and stays at 206MB.
  With them it was 541MB, over the 300MB target in ADR 0011.
- Retrieval metrics score the retriever, not the agent: precision@5 and recall@5 of the
  question against the user's labelled chunks, offline. `recon.cli retrieval propose`
  only drafts candidates, because relevance labels are the user's.

## Consequences

- The corpus changes `tool_data_snapshot`, so runs with it can't be compared with the
  current baseline. Measuring the tool's effect needs a decision about the baseline.
- The LangGraph critic node calls no tools, so the critic's grant will apply to the
  Agent SDK runtime only.
- Until part 2, the MCP server still lists the tool once a corpus is built. The Agent
  SDK roles can't call it, but LangGraph's single mode binds every listed tool, so
  build the corpus only together with part 2.
- #18's faithfulness metric isn't built. It needs the agent's answer checked against
  the passages it retrieved, which takes either a judge assertion (a rubric change) or
  storing tool outputs on `ToolCall` (a contract change).
- 10-K sections are found by their "Item N" headings. A filing that formats them
  differently contributes no 10-K text, which `fetch-text`'s chunk counts show.
