"""Turn cached filing documents into the chunks the knowledge index embeds.

Reads only what `sec_edgar_text.fetch_filing` cached, so it needs no network.
The chunks go to `knowledge_chunks.parquet` in the processed EDGAR snapshot,
next to the XBRL tables, so `tools.data_source.tool_data_snapshot_id` can hash
them with the rest of the tool data. See docs/adr/0025-retrieval-over-filing-text.md.
"""

import hashlib
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import yaml

from recon.adapters.sec_edgar_text import (
    Filing,
    filing_dir,
    html_to_text,
    read_submissions,
    select_filings,
    tenk_sections,
)

DEFAULT_KNOWLEDGE_CONFIG = Path("config/knowledge.yaml")
CHUNKS_FILENAME = "knowledge_chunks.parquet"
EARNINGS_SECTION = "earnings release"


@dataclass(frozen=True)
class KnowledgeConfig:
    """`config/knowledge.yaml`, validated."""

    embedding_model: str
    embedding_dimensions: int
    reranker_model: str
    chunk_chars: int
    chunk_overlap_chars: int
    candidates_per_search: int
    rerank_pool: int
    rrf_k: int


def load_knowledge_config(path: Path = DEFAULT_KNOWLEDGE_CONFIG) -> KnowledgeConfig:
    """Read the retrieval settings. An overlap at or above the chunk size is an error."""
    config = KnowledgeConfig(**yaml.safe_load(path.read_text(encoding="utf-8")))
    if not 0 <= config.chunk_overlap_chars < config.chunk_chars:
        raise ValueError(
            f"{path}: chunk_overlap_chars must be at least 0 and below chunk_chars."
        )
    return config


@dataclass(frozen=True)
class Chunk:
    """One passage of filing text, with what the agent needs to cite it."""

    chunk_id: str
    company_id: str
    form: str
    filed: date
    accession: str
    section: str
    text: str


def chunk_text(text: str, size: int, overlap: int) -> list[str]:
    """Split on line breaks into pieces of at most `size` characters, each
    starting `overlap` characters before the previous one ended. A single
    line longer than `size` is cut at the size."""
    pieces: list[str] = []
    current = ""
    for line in text.splitlines():
        while len(line) > size:
            pieces.extend(filter(None, [current]))
            pieces.append(line[:size])
            current, line = "", line[size - overlap :]
        if current and len(current) + 1 + len(line) > size:
            pieces.append(current)
            current = current[-overlap:] if overlap else ""
        current = f"{current}\n{line}" if current else line
    if current:
        pieces.append(current)
    return [piece.strip() for piece in pieces if piece.strip()]


def _documents(filing: Filing, raw_snapshot_dir: Path) -> dict[str, str]:
    """Section name -> text for one cached filing. Empty if nothing is cached."""
    folder = filing_dir(raw_snapshot_dir, filing)
    if filing.form == "10-K":
        path = folder / filing.primary_document
        if not path.exists():
            return {}
        return tenk_sections(html_to_text(path.read_text("utf-8", errors="replace")))
    exhibits = [
        p for p in sorted(folder.glob("*")) if not p.name.endswith("-index.htm")
    ]
    if not exhibits:
        return {}
    html = exhibits[0].read_text("utf-8", errors="replace")
    return {EARNINGS_SECTION: html_to_text(html)}


def build_chunks(
    raw_snapshot_dir: Path,
    company_ids: dict[int, str],
    cutoff: date,
    config: KnowledgeConfig,
) -> list[Chunk]:
    """Chunks of every cached filing of the companies in `company_ids` (CIK -> id)."""
    chunks: list[Chunk] = []
    for submissions in read_submissions(raw_snapshot_dir):
        company_id = company_ids.get(int(submissions["cik"]))
        if company_id is None:
            continue
        for filing in select_filings(submissions, cutoff):
            for section, text in _documents(filing, raw_snapshot_dir).items():
                pieces = chunk_text(
                    text, config.chunk_chars, config.chunk_overlap_chars
                )
                chunks.extend(
                    Chunk(
                        chunk_id=f"{filing.accession}:{section}:{n}",
                        company_id=company_id,
                        form=filing.form,
                        filed=filing.filed,
                        accession=filing.accession,
                        section=section,
                        text=piece,
                    )
                    for n, piece in enumerate(pieces)
                )
    return chunks


def write_chunks(chunks: list[Chunk], out_path: Path) -> None:
    """Write `chunks` as Parquet, sorted by id, so the same input writes the same bytes."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = [
        asdict(c) for c in sorted(chunks, key=lambda c: c.chunk_id)
    ]
    conn = duckdb.connect(":memory:")
    conn.execute(
        "CREATE TABLE chunks (chunk_id VARCHAR, company_id VARCHAR, form VARCHAR, "
        "filed DATE, accession VARCHAR, section VARCHAR, text VARCHAR)"
    )
    conn.executemany(
        "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?)",
        [tuple(row.values()) for row in rows],
    )
    target = str(out_path).replace("'", "''")
    conn.execute(
        f"COPY (SELECT * FROM chunks ORDER BY chunk_id) TO '{target}' (FORMAT parquet)"
    )


def read_chunks(path: Path) -> list[Chunk]:
    """The chunks `write_chunks` wrote, in id order."""
    target = str(path).replace("'", "''")
    rows = (
        duckdb.connect(":memory:")
        .execute(f"SELECT * FROM read_parquet('{target}') ORDER BY chunk_id")
        .fetchall()
    )
    return [Chunk(*row) for row in rows]


def corpus_id(chunks_path: Path, config: KnowledgeConfig) -> str:
    """Content hash of the chunks and the models that embed and rerank them."""
    digest = hashlib.sha256(chunks_path.read_bytes())
    digest.update(f"{config.embedding_model}|{config.reranker_model}".encode())
    return digest.hexdigest()[:12]
