"""`KnowledgeBackend`: the filing-text index and models one process searches with.

Split from `knowledge_search.py`, which holds the search itself (#104 took that
module over its size limit).
"""

import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg

from recon.adapters.knowledge_corpus import (
    chunk_count,
    corpus_id,
    load_knowledge_config,
)
from recon.contracts import ToolResult
from recon.tools.knowledge_index import connect, indexed_count, sentence_embedder
from recon.tools.knowledge_search import (
    cross_encoder_reranker,
    ranked_ids,
    search_knowledge,
)

logger = logging.getLogger(__name__)


class KnowledgeBackend:
    """Opens the index connection on the first search. The models load then
    too, unless `start_warm_up` loaded them in the background already."""

    def __init__(self, chunks_path: Path) -> None:
        self.config = load_knowledge_config()
        self.corpus = corpus_id(chunks_path, self.config)
        self.expected_chunks = chunk_count(chunks_path)
        self.embed = sentence_embedder(self.config.embedding_model)
        self.rerank = cross_encoder_reranker(self.config.reranker_model)
        self._conn: psycopg.Connection[Any] | None = None
        self._warm_up: threading.Thread | None = None

    def start_warm_up(self) -> None:
        """Load both models in a background thread (#99).

        The tool server starts per case, and loading took about 10 s of the
        first search, inside the case's wall-clock budget. Started with the
        server, it overlaps the agent's first model call instead. A case that
        never searches loads them anyway, off its critical path.
        """
        self._warm_up = threading.Thread(target=self._load_models, daemon=True)
        self._warm_up.start()

    def _load_models(self) -> None:
        try:
            self.embed(["warm up"])
            self.rerank("warm up", ["warm up"])
        except (ImportError, OSError) as exc:
            # The first search hits the same error and reports it as a status.
            logger.warning("Warming up the search models failed: %s", exc)

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

    def search(
        self, query: str, top_k: int, company_id: str | None = None
    ) -> ToolResult:
        """`search_knowledge` against this backend's index."""
        if self._warm_up is not None:
            # Waits rather than loading the models a second time alongside it.
            self._warm_up.join()
        conn = self.connection()
        return search_knowledge(
            conn,
            self.corpus if conn is not None else None,
            self.config,
            self.embed,
            self.rerank,
            query,
            top_k,
            company_id,
        )

    def variant_search(self, variant: str) -> Callable[[str, int], list[str]]:
        """A search ranked by one of `VARIANTS`, for the retrieval metrics.

        Raises when the index isn't reachable, like `chunk_ids`.
        """

        def search(query: str, top_k: int) -> list[str]:
            conn = self.connection()
            if conn is None:
                raise RuntimeError("The filing-text index isn't reachable.")
            return ranked_ids(
                conn,
                self.corpus,
                self.config,
                self.embed,
                self.rerank,
                query,
                top_k,
                variant,
            )

        return search

    def chunk_ids(
        self, query: str, top_k: int, company_id: str | None = None
    ) -> list[str]:
        """Ids of the best matching chunks, for the retrieval metrics.

        Raises when the search is unavailable, so a blip isn't scored as a
        retriever that found nothing relevant.
        """
        result = self.search(query, top_k, company_id)
        if result.status in ("unavailable", "invalid_input"):
            raise RuntimeError(result.message)
        return [row["chunk_id"] for row in result.data]
