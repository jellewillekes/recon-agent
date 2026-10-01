"""`search_knowledge`: hybrid search over SEC filing text (step 13, #18).

Dense search (pgvector cosine) and Postgres full-text search each propose
candidates. Reciprocal rank fusion merges them, and a local cross-encoder
reranks the best of the merged list. See docs/adr/0025-retrieval-over-filing-text.md.
"""

import os
import time
from collections.abc import Callable, Sequence
from functools import cache
from pathlib import Path
from typing import Any

import psycopg
from pydantic import BaseModel, Field, ValidationError

from recon.adapters.knowledge_corpus import (
    KnowledgeConfig,
    chunk_count,
    corpus_id,
    load_knowledge_config,
)
from recon.contracts import ToolResult
from recon.tools.execution import elapsed_ms, failure
from recon.tools.knowledge_index import (
    Embedder,
    connect,
    indexed_count,
    sentence_embedder,
)

# (query, passages) -> one relevance score per passage, higher is better.
Reranker = Callable[[str, Sequence[str]], list[float]]
MAX_TOP_K = 20

_COLUMNS = "chunk_id, company_id, form, filed, accession, section, text"


class SearchKnowledgeInput(BaseModel):
    """Arguments of `search_knowledge`."""

    query: str = Field(min_length=3, max_length=500)
    top_k: int = Field(default=5, ge=1, le=MAX_TOP_K)


def reciprocal_rank_fusion(rankings: Sequence[Sequence[str]], k: int) -> list[str]:
    """Merge ranked id lists: each id scores the sum of 1 / (k + rank) over
    the lists it appears in (Cormack et al., 2009). Ties keep first-seen order."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, 1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda item: -scores[item])


def _dense(
    conn: psycopg.Connection[Any], corpus: str, vector: list[float], limit: int
) -> list[str]:
    rows = conn.execute(
        "SELECT chunk_id FROM knowledge_chunks WHERE corpus_id = %s"
        " ORDER BY embedding <=> %s::vector LIMIT %s",
        (corpus, vector, limit),
    ).fetchall()
    return [row[0] for row in rows]


def _full_text(
    conn: psycopg.Connection[Any], corpus: str, query: str, limit: int
) -> list[str]:
    rows = conn.execute(
        "SELECT chunk_id FROM knowledge_chunks, websearch_to_tsquery('english', %s) q"
        " WHERE corpus_id = %s AND tsv @@ q ORDER BY ts_rank(tsv, q) DESC LIMIT %s",
        (query, corpus, limit),
    ).fetchall()
    return [row[0] for row in rows]


def _fetch(
    conn: psycopg.Connection[Any], corpus: str, ids: list[str]
) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM knowledge_chunks"
        " WHERE corpus_id = %s AND chunk_id = ANY(%s)",
        (corpus, ids),
    ).fetchall()
    names = _COLUMNS.split(", ")
    return {row[0]: dict(zip(names, row, strict=True)) for row in rows}


def search_knowledge(
    conn: psycopg.Connection[Any] | None,
    corpus: str | None,
    config: KnowledgeConfig,
    embed: Embedder,
    rerank: Reranker,
    query: str,
    top_k: int = 5,
) -> ToolResult:
    """Search SEC filing text: 8-K earnings releases (EX-99.1) and 10-K risk
    factors, MD&A and market-risk sections, filed in the two years before the
    data cutoff.

    Use it for what XBRL facts don't carry: guidance, management commentary,
    non-GAAP adjustments, segment narrative, risks. Name the company and the
    topic in `query`. Each result has the passage text plus company, form,
    filing date, accession and section, to cite.
    """
    start = time.perf_counter()
    try:
        args = SearchKnowledgeInput(query=query, top_k=top_k)
    except ValidationError as exc:
        return failure(
            start,
            "invalid_input",
            f"Invalid search_knowledge arguments: {exc.errors()[0]['msg']}. "
            f"Pass a query of 3-500 characters and top_k from 1 to {MAX_TOP_K}.",
        )
    if conn is None or corpus is None:
        return failure(
            start,
            "unavailable",
            "The filing-text index isn't available in this environment. Answer "
            "from the XBRL tools, and say that filing text couldn't be searched.",
        )
    try:
        ranked = reciprocal_rank_fusion(
            [
                _dense(
                    conn, corpus, embed([args.query])[0], config.candidates_per_search
                ),
                _full_text(conn, corpus, args.query, config.candidates_per_search),
            ],
            config.rrf_k,
        )[: config.rerank_pool]
        rows = _fetch(conn, corpus, ranked)
    except psycopg.Error as exc:
        return failure(
            start,
            "unavailable",
            f"The filing-text index failed ({type(exc).__name__}). Retry once; if "
            "it fails again, answer from the XBRL tools.",
        )
    except ImportError:
        return failure(
            start,
            "unavailable",
            "The embedding model isn't installed here (`uv sync --extra knowledge`). "
            "Answer from the XBRL tools.",
        )
    if not rows:
        return failure(
            start,
            "empty",
            "No filing text matches. Rephrase with the company name and the topic, "
            "or use the XBRL tools.",
        )
    candidates = [rows[i] for i in ranked if i in rows]
    scores = rerank(args.query, [row["text"] for row in candidates])
    order = sorted(range(len(candidates)), key=lambda i: -scores[i])[: args.top_k]
    data = [
        {
            **candidates[i],
            "filed": str(candidates[i]["filed"]),
            "score": round(scores[i], 4),
        }
        for i in order
    ]
    return ToolResult(
        status="ok",
        data=data,
        row_count=len(data),
        message=f"{len(data)} passage(s), best match first.",
        elapsed_ms=elapsed_ms(start),
    )


@cache
def _cross_encoder(name: str) -> Any:
    from sentence_transformers import CrossEncoder

    return CrossEncoder(name, device="cpu")


def cross_encoder_reranker(model_name: str) -> Reranker:
    """Rerank with a local sentence-transformers cross-encoder."""

    def rerank(query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        scores = _cross_encoder(model_name).predict([(query, p) for p in passages])
        return [float(s) for s in scores]

    return rerank


class KnowledgeBackend:
    """Opens the index connection and loads the models on the first search, so
    starting the server stays fast and a run that never searches pays nothing."""

    def __init__(self, chunks_path: Path) -> None:
        self.config = load_knowledge_config()
        self.corpus = corpus_id(chunks_path, self.config)
        self.expected_chunks = chunk_count(chunks_path)
        self.embed = sentence_embedder(self.config.embedding_model)
        self.rerank = cross_encoder_reranker(self.config.reranker_model)
        self._conn: psycopg.Connection[Any] | None = None

    def connection(self) -> psycopg.Connection[Any] | None:
        """The index connection, or None when the index isn't reachable or built."""
        if self._conn is None:
            database_url = os.environ.get("DATABASE_URL")
            if not database_url:
                return None
            try:
                conn = connect(database_url)
                # A reindex that died partway leaves fewer rows than the
                # corpus file holds. Searching that would quietly miss text.
                if indexed_count(conn, self.corpus) != self.expected_chunks:
                    conn.close()
                    return None
            except psycopg.Error:
                return None
            self._conn = conn
        return self._conn

    def search(self, query: str, top_k: int) -> ToolResult:
        """`search_knowledge` against this backend's index."""
        conn = self.connection()
        return search_knowledge(
            conn,
            self.corpus if conn is not None else None,
            self.config,
            self.embed,
            self.rerank,
            query,
            top_k,
        )

    def chunk_ids(self, query: str, top_k: int) -> list[str]:
        """Ids of the best matching chunks, for the retrieval metrics.

        Raises when the search is unavailable, so a blip isn't scored as a
        retriever that found nothing relevant.
        """
        result = self.search(query, top_k)
        if result.status in ("unavailable", "invalid_input"):
            raise RuntimeError(result.message)
        return [row["chunk_id"] for row in result.data]
