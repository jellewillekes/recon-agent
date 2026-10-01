"""The knowledge index: filing-text chunks with embeddings, in pgvector.

Lives in the existing Postgres, per the storage boundary in AGENTS.md: DuckDB
holds the read-only XBRL tables, Postgres holds embeddings. Rows carry the
`corpus_id` of the chunks file they came from, so the search tool only ever
reads the corpus the run's `tool_data_snapshot` names. See
docs/adr/0025-retrieval-over-filing-text.md.
"""

import logging
from collections.abc import Callable, Sequence
from functools import cache
from typing import Any

import psycopg
from pgvector.psycopg import register_vector

from recon.adapters.knowledge_corpus import Chunk, KnowledgeConfig

logger = logging.getLogger(__name__)

# Texts -> one vector each. Production loads a sentence-transformers model;
# tests pass a fake.
Embedder = Callable[[Sequence[str]], list[list[float]]]
_BATCH = 64


def schema_sql(dimensions: int) -> list[str]:
    """Statements that create the table and its indexes if they don't exist."""
    table = (
        "CREATE TABLE IF NOT EXISTS knowledge_chunks ("
        " corpus_id text NOT NULL, chunk_id text NOT NULL,"
        " company_id text NOT NULL, form text NOT NULL, filed date NOT NULL,"
        " accession text NOT NULL, section text NOT NULL, text text NOT NULL,"
        f" embedding vector({int(dimensions)}) NOT NULL,"
        " tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,"
        " PRIMARY KEY (corpus_id, chunk_id))"
    )
    return [
        "CREATE EXTENSION IF NOT EXISTS vector",
        table,
        "CREATE INDEX IF NOT EXISTS knowledge_chunks_tsv ON knowledge_chunks USING gin (tsv)",
        (
            "CREATE INDEX IF NOT EXISTS knowledge_chunks_embedding "
            "ON knowledge_chunks USING hnsw (embedding vector_cosine_ops)"
        ),
    ]


def connect(database_url: str) -> psycopg.Connection[Any]:
    """A connection with pgvector's types registered."""
    conn = psycopg.connect(database_url, autocommit=True)
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    register_vector(conn)
    return conn


def indexed_count(conn: psycopg.Connection[Any], corpus: str) -> int:
    """How many chunks of `corpus` are in the index."""
    row = conn.execute(
        "SELECT count(*) FROM knowledge_chunks WHERE corpus_id = %s", (corpus,)
    ).fetchone()
    return int(row[0]) if row else 0


def build_index(
    conn: psycopg.Connection[Any],
    chunks: list[Chunk],
    corpus: str,
    config: KnowledgeConfig,
    embed: Embedder,
) -> int:
    """Embed and store `chunks` as `corpus`, then drop other corpora.

    Skips the work when the corpus is already fully indexed. Returns the
    number of chunks indexed.
    """
    for statement in schema_sql(config.embedding_dimensions):
        conn.execute(statement)
    if indexed_count(conn, corpus) == len(chunks):
        logger.info("Corpus %s is already indexed (%d chunks).", corpus, len(chunks))
        return len(chunks)
    conn.execute("DELETE FROM knowledge_chunks WHERE corpus_id = %s", (corpus,))
    for start in range(0, len(chunks), _BATCH):
        batch = chunks[start : start + _BATCH]
        vectors = embed([chunk.text for chunk in batch])
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO knowledge_chunks (corpus_id, chunk_id, company_id, form,"
                " filed, accession, section, text, embedding)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        corpus,
                        c.chunk_id,
                        c.company_id,
                        c.form,
                        c.filed,
                        c.accession,
                        c.section,
                        c.text,
                        vector,
                    )
                    for c, vector in zip(batch, vectors, strict=True)
                ],
            )
        logger.info("Indexed %d of %d chunks.", start + len(batch), len(chunks))
    conn.execute("DELETE FROM knowledge_chunks WHERE corpus_id <> %s", (corpus,))
    return len(chunks)


@cache
def _sentence_model(name: str) -> Any:
    # Imported on first use: loading torch takes seconds, and most processes
    # (tests, the XBRL tools, the CLI's other commands) never need it.
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(name, device="cpu")


def sentence_embedder(model_name: str) -> Embedder:
    """Embed with a local sentence-transformers model, normalized for cosine."""

    def embed(texts: Sequence[str]) -> list[list[float]]:
        vectors = _sentence_model(model_name).encode(
            list(texts), normalize_embeddings=True, batch_size=_BATCH
        )
        return [list(map(float, v)) for v in vectors]

    return embed
