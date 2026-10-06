"""`search_knowledge`: hybrid search over SEC filing text (step 13, #18).

Dense search (pgvector cosine) and Postgres full-text search each propose
candidates. Reciprocal rank fusion merges them, and a local cross-encoder
reranks the best of the merged list. See docs/adr/0025-retrieval-over-filing-text.md.
"""

import logging
import time
from collections.abc import Callable, Sequence
from functools import cache
from typing import Any

import psycopg
from pydantic import BaseModel, Field, ValidationError, field_validator

from recon.adapters.knowledge_corpus import (
    KnowledgeConfig,
)
from recon.contracts import ToolResult
from recon.tools.execution import elapsed_ms, failure
from recon.tools.knowledge_index import (
    Embedder,
)

logger = logging.getLogger(__name__)

# (query, passages) -> one relevance score per passage, higher is better.
Reranker = Callable[[str, Sequence[str]], list[float]]
MAX_TOP_K = 20

# The search tool's own ranking is "hybrid_rerank". The others are the parts
# it's built from, ranked alone, for the retrieval metrics (#18).
VARIANTS = ("dense", "full_text", "hybrid", "hybrid_rerank")

_COLUMNS = "chunk_id, company_id, form, filed, accession, section, text"


class SearchKnowledgeInput(BaseModel):
    """Arguments of `search_knowledge`."""

    query: str = Field(min_length=3, max_length=500)
    top_k: int = Field(default=5, ge=1, le=MAX_TOP_K)
    # A company id as search_companies returns it (a ticker), upper-cased.
    company_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9.\-]{1,10}$")

    @field_validator("company_id")
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        return value.upper() if value is not None else None


def _company_clause(company_id: str | None) -> tuple[str, tuple[str, ...]]:
    """SQL and parameters restricting candidates to one company (#104)."""
    return ("", ()) if company_id is None else (" AND company_id = %s", (company_id,))


def reciprocal_rank_fusion(rankings: Sequence[Sequence[str]], k: int) -> list[str]:
    """Merge ranked id lists: each id scores the sum of 1 / (k + rank) over
    the lists it appears in (Cormack et al., 2009). Ties keep first-seen order."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, 1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda item: -scores[item])


def _dense(
    conn: psycopg.Connection[Any],
    corpus: str,
    vector: list[float],
    limit: int,
    company_id: str | None = None,
) -> list[str]:
    company, company_params = _company_clause(company_id)
    rows = conn.execute(
        f"SELECT chunk_id FROM knowledge_chunks WHERE corpus_id = %s{company}"
        " ORDER BY embedding <=> %s::vector LIMIT %s",
        (corpus, *company_params, vector, limit),
    ).fetchall()
    return [row[0] for row in rows]


# The query's terms ORed, not ANDed: a question names the company and other
# words a passage rarely repeats all of, so requiring every term matched
# almost nothing (#98). ts_rank still puts passages matching more terms first.
_ANY_TERM = (
    "CAST(replace(plainto_tsquery('english', %s)::text, ' & ', ' | ') AS tsquery)"
)


def _full_text(
    conn: psycopg.Connection[Any],
    corpus: str,
    query: str,
    limit: int,
    company_id: str | None = None,
) -> list[str]:
    company, company_params = _company_clause(company_id)
    rows = conn.execute(
        f"SELECT chunk_id FROM knowledge_chunks, {_ANY_TERM} q"
        f" WHERE corpus_id = %s{company} AND tsv @@ q"
        " ORDER BY ts_rank(tsv, q) DESC LIMIT %s",
        (query, corpus, *company_params, limit),
    ).fetchall()
    return [row[0] for row in rows]


def _fused(
    conn: psycopg.Connection[Any],
    corpus: str,
    config: KnowledgeConfig,
    embed: Embedder,
    query: str,
    company_id: str | None = None,
) -> list[str]:
    """Dense and full-text candidates merged by reciprocal rank fusion."""
    limit = config.candidates_per_search
    return reciprocal_rank_fusion(
        [
            _dense(conn, corpus, embed([query])[0], limit, company_id),
            _full_text(conn, corpus, query, limit, company_id),
        ],
        config.rrf_k,
    )


def ranked_ids(
    conn: psycopg.Connection[Any],
    corpus: str,
    config: KnowledgeConfig,
    embed: Embedder,
    rerank: Reranker,
    query: str,
    top_k: int,
    variant: str,
) -> list[str]:
    """The top `top_k` chunk ids for `query` under one of `VARIANTS`.

    "hybrid_rerank" goes through `search_knowledge` itself, so it can't drift
    from what the tool returns. Raises RuntimeError on a search failure.
    """
    # Each variant ranks the same candidate pools the tool fuses.
    try:
        if variant == "dense":
            vector = embed([query])[0]
            return _dense(conn, corpus, vector, config.candidates_per_search)[:top_k]
        if variant == "full_text":
            return _full_text(conn, corpus, query, config.candidates_per_search)[:top_k]
        if variant == "hybrid":
            return _fused(conn, corpus, config, embed, query)[:top_k]
    except (psycopg.Error, ImportError, OSError) as exc:
        raise RuntimeError(
            f"The {variant} search failed ({type(exc).__name__})."
        ) from exc
    if variant == "hybrid_rerank":
        result = search_knowledge(conn, corpus, config, embed, rerank, query, top_k)
        if result.status in ("unavailable", "invalid_input"):
            raise RuntimeError(result.message)
        return [row["chunk_id"] for row in result.data]
    raise ValueError(f"Unknown search variant {variant!r}. Use one of {VARIANTS}.")


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
    company_id: str | None = None,
) -> ToolResult:
    """Search SEC filing text: 8-K earnings releases (EX-99.1) and 10-K risk
    factors, MD&A and market-risk sections, filed in the two years before the
    data cutoff.

    Use it for what XBRL facts don't carry: guidance, management commentary,
    non-GAAP adjustments, segment narrative, risks. Name the company and the
    topic in `query`. Pass `company_id` (as `search_companies` returns it) to
    search only that company's filings. Each result has the passage text plus
    company, form, filing date, accession and section, to cite.
    """
    start = time.perf_counter()
    try:
        args = SearchKnowledgeInput(query=query, top_k=top_k, company_id=company_id)
    except ValidationError as exc:
        return failure(
            start,
            "invalid_input",
            f"Invalid search_knowledge arguments: {exc.errors()[0]['msg']}. "
            f"Pass a query of 3-500 characters, top_k from 1 to {MAX_TOP_K}, and "
            "company_id as search_companies returns it, or no company_id.",
        )
    if conn is None or corpus is None:
        return failure(
            start,
            "unavailable",
            "The filing-text index isn't available in this environment. Answer "
            "from the XBRL tools, and say that filing text couldn't be searched.",
        )
    try:
        ranked = _fused(conn, corpus, config, embed, args.query, args.company_id)[
            : config.rerank_pool
        ]
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
    except OSError as exc:
        # E.g. the model isn't cached yet and can't be downloaded.
        return failure(
            start,
            "unavailable",
            f"The embedding model failed to load ({type(exc).__name__}). Answer "
            "from the XBRL tools.",
        )
    if not rows:
        widen = (
            f"No filing text from {args.company_id} matches. Check the id with "
            "search_companies, or search without company_id."
            if args.company_id
            else "No filing text matches. Rephrase with the company name and the "
            "topic, or use the XBRL tools."
        )
        return failure(start, "empty", widen)
    candidates = [rows[i] for i in ranked if i in rows]
    try:
        scores = rerank(args.query, [row["text"] for row in candidates])
    except (ImportError, OSError) as exc:
        return failure(
            start,
            "unavailable",
            f"The reranker failed ({type(exc).__name__}). Retry once; if it fails "
            "again, answer from the XBRL tools.",
        )
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
