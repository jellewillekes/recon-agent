"""Retrieval over filing text (step 13, #18): fetch, chunk, index, search, score.

Offline: fake SEC client, fake models and a fake database connection. All
companies and filings here are fictional. `test_pgvector_round_trip` checks
the SQL against a real Postgres with pgvector, and runs only when
RECON_TEST_DATABASE_URL points at one (e.g. the compose stack's).
"""

import asyncio
import hashlib
import json
import os
import time
from datetime import UTC, date, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any, Self

import psycopg
import pytest

from recon.adapters import knowledge_corpus as corpus
from recon.adapters import sec_edgar_text as text
from recon.contracts import Case, EvalRun
from recon.eval import retrieval
from recon.tools import (
    data_source,
    knowledge_backend,
    knowledge_index,
    knowledge_search,
    mcp_server,
)

ROOT = Path(__file__).resolve().parent.parent
CONFIG = corpus.load_knowledge_config(ROOT / "config" / "knowledge.yaml")
CUTOFF = date(2025, 4, 7)

pytestmark = pytest.mark.unit


# --- fetching filing text ---------------------------------------------------


def _submissions(cik: int = 1234) -> dict[str, Any]:
    rows = [
        ("0001-25-000001", "8-K", "2025-02-01", "2.02,9.01", "q4.htm"),
        ("0001-25-000002", "8-K", "2025-01-15", "5.02", "officer.htm"),
        ("0001-25-000003", "10-K", "2025-02-20", "", "annual.htm"),
        ("0001-25-000004", "8-K", "2025-05-01", "2.02", "late.htm"),
        ("0001-22-000005", "10-K", "2022-02-20", "", "old.htm"),
    ]
    keys = ["accessionNumber", "form", "filingDate", "items", "primaryDocument"]
    return {
        "cik": str(cik),
        "filings": {"recent": {k: [r[i] for r in rows] for i, k in enumerate(keys)}},
    }


def test_select_filings_keeps_results_8ks_and_10ks_in_the_window() -> None:
    picked = text.select_filings(_submissions(), CUTOFF)
    assert [(f.accession, f.form) for f in picked] == [
        ("0001-25-000003", "10-K"),
        ("0001-25-000001", "8-K"),
    ]


INDEX_HTML = """<table class="tableFile">
<tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
<tr><td>1</td><td>8-K</td><td><a href="/Archives/edgar/data/1234/000125000001/q4.htm">q4.htm</a></td><td>8-K</td><td>10</td></tr>
<tr><td>2</td><td>PRESS RELEASE</td><td><a href="/Archives/edgar/data/1234/000125000001/ex991.htm">ex991.htm</a></td><td>EX-99.1</td><td>20</td></tr>
</table>"""


def test_exhibit_99_1_is_found_by_its_type_column() -> None:
    assert text.exhibit_99_1(INDEX_HTML) == "ex991.htm"
    assert text.exhibit_99_1(INDEX_HTML.replace("EX-99.1", "EX-10.1")) is None


class _FakeEdgar:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.urls: list[str] = []

    def get(self, url: str) -> Any:
        self.urls.append(url)
        if url not in self.pages:
            return None
        return type("R", (), {"content": self.pages[url].encode()})()


def test_fetch_filing_follows_the_index_to_the_exhibit(tmp_path: Path) -> None:
    filing = text.select_filings(_submissions(), CUTOFF)[1]
    base = "https://www.sec.gov/Archives/edgar/data/1234/000125000001/"
    edgar = _FakeEdgar(
        {
            base + "0001-25-000001-index.htm": INDEX_HTML,
            base + "ex991.htm": "<p>Revenue rose.</p>",
        }
    )
    path = text.fetch_filing(edgar, filing, tmp_path)  # type: ignore[arg-type]
    assert path is not None and path.name == "ex991.htm"
    assert path.read_text() == "<p>Revenue rose.</p>"
    text.fetch_filing(edgar, filing, tmp_path)  # type: ignore[arg-type]
    assert len(edgar.urls) == 2, "a second fetch is served from the cache"


def test_html_to_text_drops_markup_scripts_and_the_xbrl_header() -> None:
    html = (
        "<html><head><title>t</title></head><body><ix:header>hidden</ix:header>"
        "<script>x()</script><p>Net   revenue was</p><div>$10 million.</div></body></html>"
    )
    assert text.html_to_text(html) == "Net revenue was\n$10 million."


def test_tenk_sections_use_the_body_headings_not_the_table_of_contents() -> None:
    body = "x" * 300
    doc = "\n".join(
        [
            "Item 1A. Risk Factors",
            "Item 7. Management's Discussion",
            "Item 8. Statements",
            f"Item 1A. Risk Factors\nRisks {body}",
            "Item 1B. Unresolved Staff Comments\nNone.",
            f"Item 7. Management's Discussion and Analysis\nDiscussion {body}",
            f"Item 7A. Quantitative and Qualitative Disclosures\nRates {body}",
            "Item 8. Financial Statements\nTables.",
        ]
    )
    sections = text.tenk_sections(doc)
    assert set(sections) == {"risk factors", "md&a", "market risk"}
    assert sections["risk factors"].startswith("Item 1A. Risk Factors\nRisks")
    assert "Unresolved" not in sections["risk factors"]
    assert "Rates" not in sections["md&a"]


# --- chunks -------------------------------------------------------------------


def test_chunks_respect_the_size_and_overlap() -> None:
    lines = [f"line {i:03d} " + "w" * 40 for i in range(40)]
    pieces = corpus.chunk_text("\n".join(lines), size=300, overlap=60)
    assert all(len(p) <= 300 for p in pieces)
    assert all(a[-40:] in b for a, b in pairwise(pieces))
    assert "line 000" in pieces[0] and "line 039" in pieces[-1]


def test_the_carry_over_never_pushes_a_chunk_over_its_size() -> None:
    """Review of #91: a long line right after a boundary plus the carried-over
    overlap made a 1,251-character chunk at size 1,200."""
    pieces = corpus.chunk_text("a" * 1100 + "\n" + "b" * 1050, size=1200, overlap=200)
    assert max(len(p) for p in pieces) <= 1200
    assert pieces == ["a" * 1100, "b" * 1050]


def test_a_line_longer_than_a_chunk_is_cut() -> None:
    pieces = corpus.chunk_text("a" * 700, size=300, overlap=50)
    assert [len(p) for p in pieces] == [300, 300, 200]


def test_a_line_that_would_overflow_with_the_carry_stays_within_size() -> None:
    first = "x" * 290
    second = "y" * 280
    pieces = corpus.chunk_text(f"{first}\n{second}", size=300, overlap=60)
    assert all(len(p) <= 300 for p in pieces)


def _raw_snapshot(tmp_path: Path) -> Path:
    raw = tmp_path / "raw"
    subs = _submissions()
    (raw / "submissions").mkdir(parents=True)
    (raw / "submissions" / "CIK0000001234.json").write_text(json.dumps(subs))
    filings = text.select_filings(subs, CUTOFF)
    tenk, eightk = filings[0], filings[1]
    folder = text.filing_dir(raw, tenk)
    folder.mkdir(parents=True)
    risks = "Supply concentration is a risk. " * 20
    (folder / "annual.htm").write_text(
        f"<p>Item 1A. Risk Factors</p><p>{risks}</p><p>Item 1B. None</p>"
    )
    folder = text.filing_dir(raw, eightk)
    folder.mkdir(parents=True)
    (folder / "0001-25-000001-index.htm").write_text(INDEX_HTML)
    (folder / "ex991.htm").write_text("<p>Quarterly revenue was $12 million.</p>")
    return raw


def test_build_chunks_reads_only_cached_files(tmp_path: Path) -> None:
    chunks = corpus.build_chunks(
        _raw_snapshot(tmp_path), {1234: "FICT"}, CUTOFF, CONFIG
    )
    assert {(c.form, c.section) for c in chunks} == {
        ("10-K", "risk factors"),
        ("8-K", corpus.EARNINGS_SECTION),
    }
    assert all(c.company_id == "FICT" for c in chunks)
    release = next(c for c in chunks if c.form == "8-K")
    assert release.text == "Quarterly revenue was $12 million."
    assert release.chunk_id == "0001-25-000001:earnings release:0"
    assert corpus.build_chunks(_raw_snapshot(tmp_path / "b"), {}, CUTOFF, CONFIG) == []


def test_the_chunks_file_round_trips_and_is_byte_stable(tmp_path: Path) -> None:
    chunks = corpus.build_chunks(
        _raw_snapshot(tmp_path), {1234: "FICT"}, CUTOFF, CONFIG
    )
    first, second = tmp_path / "a.parquet", tmp_path / "b.parquet"
    corpus.write_chunks(chunks, first)
    corpus.write_chunks(list(reversed(chunks)), second)
    assert first.read_bytes() == second.read_bytes()
    assert corpus.read_chunks(first) == sorted(chunks, key=lambda c: c.chunk_id)
    other_model = corpus.KnowledgeConfig(
        **{**CONFIG.__dict__, "reranker_model": "other"}
    )
    assert corpus.corpus_id(first, CONFIG) != corpus.corpus_id(first, other_model)


def test_a_built_corpus_changes_the_tool_data_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(data_source.TOOL_DATA_ENV, "edgar")
    monkeypatch.chdir(ROOT)
    snapshot = tmp_path / "20250101"
    snapshot.mkdir()
    (snapshot / "manifest.json").write_text("{}")
    for table in data_source.EDGAR_TABLES:
        (snapshot / f"{table}.parquet").write_bytes(table.encode())
    before = data_source.tool_data_snapshot_id(tmp_path)
    assert data_source.knowledge_chunks_path(tmp_path) is None
    chunks = corpus.build_chunks(
        _raw_snapshot(tmp_path), {1234: "FICT"}, CUTOFF, CONFIG
    )
    corpus.write_chunks(chunks, snapshot / corpus.CHUNKS_FILENAME)
    after = data_source.tool_data_snapshot_id(tmp_path)
    assert after.startswith(before + "-k")


@pytest.mark.parametrize(
    "setting", ["search_version", "candidates_per_search", "rerank_pool", "rrf_k"]
)
def test_a_search_setting_changes_the_tool_data_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setting: str
) -> None:
    """#105: ranking changes what the agent reads, so the gate must see it.
    The corpus id is the index's key and stays the same."""
    monkeypatch.setenv(data_source.TOOL_DATA_ENV, "edgar")
    monkeypatch.chdir(ROOT)
    snapshot = tmp_path / "20250101"
    snapshot.mkdir()
    (snapshot / "manifest.json").write_text("{}")
    for table in data_source.EDGAR_TABLES:
        (snapshot / f"{table}.parquet").write_bytes(table.encode())
    chunks = corpus.build_chunks(
        _raw_snapshot(tmp_path), {1234: "FICT"}, CUTOFF, CONFIG
    )
    corpus.write_chunks(chunks, snapshot / corpus.CHUNKS_FILENAME)
    changed = corpus.KnowledgeConfig(
        **{**CONFIG.__dict__, setting: getattr(CONFIG, setting) + 1}
    )
    before = data_source.tool_data_snapshot_id(tmp_path)
    monkeypatch.setattr(data_source, "load_knowledge_config", lambda: changed)
    after = data_source.tool_data_snapshot_id(tmp_path)
    assert before != after
    chunks_path = snapshot / corpus.CHUNKS_FILENAME
    assert corpus.corpus_id(chunks_path, CONFIG) == corpus.corpus_id(
        chunks_path, changed
    )


def _eval_run() -> EvalRun:
    return EvalRun(
        run_id="eval-1",
        timestamp_utc=datetime(2026, 1, 1, tzinfo=UTC),
        dataset="finance-agent-bench",
        dataset_license="MIT",
        dataset_attribution="attribution",
        runtime="agent_sdk",
        mode="single",
        model_config_hash="abc",
        prompt_hashes={"investigator": "abc"},
        rubric_version="1",
        case_scores=[],
        aggregate={},
        total_cost_eur=0.0,
    )


def test_retrieval_metrics_record_which_labels_scored_them(tmp_path: Path) -> None:
    """#105: retrieval metrics compare only on the same label set."""
    labels = tmp_path / "labels.yaml"
    labels.write_text("c1: [a]\n")
    first = retrieval.labels_hash(labels)
    labels.write_text("c1: [a, b]\n")
    assert retrieval.labels_hash(labels) != first
    run = _eval_run()
    assert (
        retrieval.with_retrieval_metrics(run, {}, labels).retrieval_labels_hash is None
    )
    scored = retrieval.with_retrieval_metrics(
        run, {"retrieval_recall_at_5": 1.0}, labels
    )
    assert scored.retrieval_labels_hash == retrieval.labels_hash(labels)


# --- search ---------------------------------------------------------------------


def test_reciprocal_rank_fusion_rewards_agreement() -> None:
    fused = knowledge_search.reciprocal_rank_fusion(
        [["a", "b", "c"], ["c", "d", "a"]], k=60
    )
    assert fused[:2] == ["a", "c"]
    assert set(fused) == {"a", "b", "c", "d"}


ROWS = {
    f"c{i}": (f"c{i}", "FICT", "8-K", date(2025, 2, 1), "acc", "earnings release", t)
    for i, t in enumerate(
        ["revenue grew", "guidance raised", "risk of supply", "margin fell"]
    )
}


class _FakeConn:
    """Answers the three search queries from ROWS; dense and full-text disagree."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def execute(self, sql: str, params: tuple[Any, ...]) -> Any:
        if self.fail:
            raise psycopg.OperationalError("connection lost")
        if "embedding <=>" in sql:
            ids = ["c0", "c1", "c2"]
        elif "tsv @@" in sql:
            ids = ["c3", "c1"]
        else:
            return _Rows([ROWS[i] for i in params[1]])
        return _Rows([(i,) for i in ids])


class _Rows:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


def _embed(texts: Any) -> list[list[float]]:
    return [[1.0, 0.0] for _ in texts]


def _rerank(query: str, passages: Any) -> list[float]:
    return [1.0 if "guidance" in p else 0.5 if "margin" in p else 0.0 for p in passages]


def _search(conn: Any, query: str = "fictional guidance", top_k: int = 2) -> Any:
    return knowledge_search.search_knowledge(
        conn, "corpus", CONFIG, _embed, _rerank, query, top_k
    )


def test_search_reranks_the_fused_candidates() -> None:
    result = _search(_FakeConn())
    assert result.status == "ok"
    assert [row["chunk_id"] for row in result.data] == ["c1", "c3"]
    assert result.data[0]["filed"] == "2025-02-01"
    assert {"company_id", "form", "accession", "section", "text", "score"} <= set(
        result.data[0]
    )


@pytest.mark.parametrize(
    ("conn", "query", "top_k", "status"),
    [
        (_FakeConn(), "ab", 2, "invalid_input"),
        (_FakeConn(), "fictional guidance", 99, "invalid_input"),
        (None, "fictional guidance", 2, "unavailable"),
        (_FakeConn(fail=True), "fictional guidance", 2, "unavailable"),
    ],
    ids=["short-query", "top-k-too-high", "no-index", "db-error"],
)
def test_search_reports_each_failure_as_a_status(
    conn: Any,
    query: str,
    top_k: int,
    status: str,
) -> None:
    result = _search(conn, query, top_k)
    assert result.status == status
    assert result.data == [] and result.message


def test_search_without_the_model_installed_is_unavailable() -> None:
    def missing(texts: Any) -> list[list[float]]:
        raise ImportError("No module named 'sentence_transformers'")

    result = knowledge_search.search_knowledge(
        _FakeConn(),  # type: ignore[arg-type]
        "corpus",
        CONFIG,
        missing,
        _rerank,
        "fictional guidance",
        2,
    )
    assert result.status == "unavailable"
    assert "--extra knowledge" in result.message


def test_search_reports_reranker_failure_as_unavailable() -> None:
    def broken(query: str, passages: Any) -> list[float]:
        raise OSError("no network: couldn't download the cross-encoder model")

    result = knowledge_search.search_knowledge(
        _FakeConn(),  # type: ignore[arg-type]
        "corpus",
        CONFIG,
        _embed,
        broken,
        "fictional guidance",
        2,
    )
    assert result.status == "unavailable"
    assert result.data == [] and result.message


def test_search_with_no_match_is_empty() -> None:
    class _Nothing(_FakeConn):
        def execute(self, sql: str, params: tuple[Any, ...]) -> Any:
            return _Rows([])

    assert _search(_Nothing()).status == "empty"


class _FilterRecordingConn(_FakeConn):
    """Records the candidate queries, to check the company filter reaches them."""

    def __init__(self) -> None:
        super().__init__()
        self.candidate_queries: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params: tuple[Any, ...]) -> Any:
        if "embedding <=>" in sql or "tsv @@" in sql:
            self.candidate_queries.append((sql, params))
        return super().execute(sql, params)


def test_a_company_filter_restricts_both_candidate_searches() -> None:
    """#104: a question naming a company by ticker got passages from others."""
    conn = _FilterRecordingConn()
    result = knowledge_search.search_knowledge(
        conn,  # type: ignore[arg-type]
        "corpus",
        CONFIG,
        _embed,
        _rerank,
        "fictional guidance",
        2,
        "fict",
    )
    assert result.status == "ok"
    assert len(conn.candidate_queries) == 2
    for sql, params in conn.candidate_queries:
        assert "company_id = %s" in sql
        assert "FICT" in params


def test_without_a_company_the_search_is_unfiltered() -> None:
    conn = _FilterRecordingConn()
    _search(conn)
    assert all("company_id" not in sql for sql, _ in conn.candidate_queries)


def test_a_malformed_company_id_is_invalid_input() -> None:
    result = knowledge_search.search_knowledge(
        _FakeConn(),  # type: ignore[arg-type]
        "corpus",
        CONFIG,
        _embed,
        _rerank,
        "fictional guidance",
        2,
        "no such id!",
    )
    assert result.status == "invalid_input"
    assert "search_companies" in result.message


def test_a_company_without_matching_text_says_how_to_widen_the_search() -> None:
    class _Nothing(_FakeConn):
        def execute(self, sql: str, params: tuple[Any, ...]) -> Any:
            return _Rows([])

    result = knowledge_search.search_knowledge(
        _Nothing(),  # type: ignore[arg-type]
        "corpus",
        CONFIG,
        _embed,
        _rerank,
        "fictional guidance",
        2,
        "FICT",
    )
    assert result.status == "empty"
    assert "without company_id" in result.message


def test_the_server_tool_takes_a_company_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(ROOT)
    chunks_path = tmp_path / corpus.CHUNKS_FILENAME
    corpus.write_chunks(
        corpus.build_chunks(_raw_snapshot(tmp_path), {1234: "FICT"}, CUTOFF, CONFIG),
        chunks_path,
    )
    server = mcp_server.build_server(data_source.open_tool_data(), chunks_path)
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert "company_id" in tools["search_knowledge_tool"].inputSchema["properties"]


class _RecordingConn:
    def __init__(self, already: int) -> None:
        self.already = already
        self.statements: list[str] = []
        self.inserted: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        self.statements.append(sql)
        return type("R", (), {"fetchone": lambda _self: (self.already,)})()

    def cursor(self) -> Any:
        outer = self

        class _Cursor:
            def __enter__(self) -> Self:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
                outer.inserted.extend(rows)

        return _Cursor()


def test_build_index_embeds_new_chunks_and_drops_other_corpora(tmp_path: Path) -> None:
    chunks = corpus.build_chunks(
        _raw_snapshot(tmp_path), {1234: "FICT"}, CUTOFF, CONFIG
    )
    conn = _RecordingConn(already=0)
    count = knowledge_index.build_index(conn, chunks, "corp1", CONFIG, _embed)  # type: ignore[arg-type]
    assert count == len(chunks) == len(conn.inserted)
    assert conn.statements[-1].startswith(
        "DELETE FROM knowledge_chunks WHERE corpus_id <>"
    )
    skip = _RecordingConn(already=len(chunks))
    knowledge_index.build_index(skip, chunks, "corp1", CONFIG, _embed)  # type: ignore[arg-type]
    assert skip.inserted == []


def test_the_server_offers_search_knowledge_only_with_a_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(ROOT)
    conn = data_source.open_tool_data()

    def names(server: Any) -> set[str]:
        return {tool.name for tool in asyncio.run(server.list_tools())}

    assert "search_knowledge_tool" not in names(mcp_server.build_server(conn))
    chunks_path = tmp_path / corpus.CHUNKS_FILENAME
    corpus.write_chunks(
        corpus.build_chunks(_raw_snapshot(tmp_path), {1234: "FICT"}, CUTOFF, CONFIG),
        chunks_path,
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)
    server = mcp_server.build_server(conn, chunks_path)
    assert "search_knowledge_tool" in names(server)
    # The tool returns backend.search(); without DATABASE_URL that's unavailable.
    backend = knowledge_backend.KnowledgeBackend(chunks_path)
    assert backend.search("fictional revenue", 3).status == "unavailable"


# --- metrics ----------------------------------------------------------------------


def test_precision_and_recall_at_k() -> None:
    retrieved = ["a", "x", "b", "y", "z", "c"]
    assert retrieval.precision_at_k(retrieved, {"a", "b", "c"}) == pytest.approx(2 / 5)
    assert retrieval.recall_at_k(retrieved, {"a", "b", "c"}) == pytest.approx(2 / 3)
    assert retrieval.recall_at_k(retrieved, set()) == 1.0


def _case(case_id: str, question: str) -> Case:
    return Case(
        case_id=case_id,
        source="finance-agent-bench",
        question=question,
        expected_answer="a",
        expected_tool_path=None,
        context={"rubric": []},
        tags=[],
        license="MIT",
        attribution="attribution",
    )


def test_retrieval_metrics_cover_only_labelled_cases(tmp_path: Path) -> None:
    labels_path = tmp_path / "labels.yaml"
    labels_path.write_text("q1: [a, b]\n")
    labels = retrieval.load_labels(labels_path)
    cases = [_case("q1", "first?"), _case("q2", "second?")]
    asked: list[str] = []

    def search(query: str, k: int) -> list[str]:
        asked.append(query)
        return ["a", "z", "y", "x", "w"]

    metrics = retrieval.retrieval_metrics(labels, cases, search)
    assert asked == ["first?"]
    assert metrics == {
        "retrieval_labelled_cases": 1.0,
        "retrieval_precision_at_5": pytest.approx(0.2),
        "retrieval_recall_at_5": pytest.approx(0.5),
    }
    assert retrieval.load_labels(tmp_path / "missing.yaml") == {}
    assert retrieval.retrieval_metrics({}, cases, search) == {}


# --- real pgvector ------------------------------------------------------------------


def _hash_embed(texts: Any) -> list[list[float]]:
    vectors = []
    for t in texts:
        digest = hashlib.sha256(t.encode()).digest()
        raw = [digest[i % 32] / 255 for i in range(CONFIG.embedding_dimensions)]
        norm = sum(x * x for x in raw) ** 0.5
        vectors.append([x / norm for x in raw])
    return vectors


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("RECON_TEST_DATABASE_URL"),
    reason="needs RECON_TEST_DATABASE_URL pointing at Postgres",
)
def test_full_text_matches_passages_holding_only_some_query_terms() -> None:
    """#98: a question names words a passage doesn't repeat, so requiring every
    term matched almost nothing. Any term may match; more terms rank higher."""
    sql = (
        f"SELECT to_tsvector('english', %s) @@ q, ts_rank(to_tsvector('english', %s), q)"
        f" FROM {knowledge_search._ANY_TERM} q"
    )
    query = "When does Fictional Corp expect production to begin?"
    with knowledge_index.connect(os.environ["RECON_TEST_DATABASE_URL"]) as conn:

        def match(passage: str) -> tuple[bool, float]:
            row = conn.execute(sql, (passage, passage, query)).fetchone()
            assert row is not None
            return row[0], row[1]

        some, rank_some = match("Production begins in 2025.")
        more, rank_more = match("Fictional expects production to begin in 2025.")
        none, _ = match("Revenue grew.")
    assert some and more and not none
    assert rank_more > rank_some


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("RECON_TEST_DATABASE_URL"),
    reason="needs RECON_TEST_DATABASE_URL pointing at Postgres with pgvector",
)
def test_pgvector_round_trip(tmp_path: Path) -> None:
    chunks = corpus.build_chunks(
        _raw_snapshot(tmp_path), {1234: "FICT"}, CUTOFF, CONFIG
    )
    with knowledge_index.connect(os.environ["RECON_TEST_DATABASE_URL"]) as conn:
        corpus_name = "test-corpus"
        knowledge_index.build_index(conn, chunks, corpus_name, CONFIG, _hash_embed)
        assert knowledge_index.indexed_count(conn, corpus_name) == len(chunks)
        result = knowledge_search.search_knowledge(
            conn, corpus_name, CONFIG, _hash_embed, _rerank, "quarterly revenue", 3
        )
        assert result.status == "ok"
        assert any("Quarterly revenue" in row["text"] for row in result.data)
        conn.execute(
            "DELETE FROM knowledge_chunks WHERE corpus_id = %s", (corpus_name,)
        )


def test_a_long_line_starting_with_item_is_not_a_heading() -> None:
    body = "x" * 300
    cross_reference = (
        "Item 7 of this report describes how rates affect the fictional company's "
        "funding costs across every period presented in the statements."
    )
    doc = "\n".join(
        [
            f"Item 7. Management's Discussion and Analysis\nDiscussion {body}",
            cross_reference,
            f"More discussion {body}",
            "Item 8. Financial Statements\nTables.",
        ]
    )
    section = text.tenk_sections(doc)["md&a"]
    assert section.startswith("Item 7. Management's Discussion")
    assert cross_reference in section


def test_a_partly_built_index_is_not_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(ROOT)
    chunks_path = tmp_path / corpus.CHUNKS_FILENAME
    chunks = corpus.build_chunks(
        _raw_snapshot(tmp_path), {1234: "FICT"}, CUTOFF, CONFIG
    )
    corpus.write_chunks(chunks, chunks_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")

    class _Conn:
        closed = False

        def close(self) -> None:
            self.closed = True

    rows = {"n": len(chunks) - 1}
    monkeypatch.setattr(knowledge_backend, "connect", lambda url: _Conn())
    monkeypatch.setattr(knowledge_backend, "indexed_count", lambda conn, c: rows["n"])
    assert knowledge_backend.KnowledgeBackend(chunks_path).connection() is None
    rows["n"] = len(chunks)
    assert knowledge_backend.KnowledgeBackend(chunks_path).connection() is not None


def test_metrics_stop_when_a_search_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An index blip must not be scored as a retriever that found nothing."""
    monkeypatch.chdir(ROOT)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    chunks_path = tmp_path / corpus.CHUNKS_FILENAME
    corpus.write_chunks(
        corpus.build_chunks(_raw_snapshot(tmp_path), {1234: "FICT"}, CUTOFF, CONFIG),
        chunks_path,
    )
    backend = knowledge_backend.KnowledgeBackend(chunks_path)
    with pytest.raises(RuntimeError, match="isn't available"):
        retrieval.retrieval_metrics(
            {"q1": ["a"]}, [_case("q1", "first?")], backend.chunk_ids
        )


def test_an_embedding_model_that_cannot_load_is_unavailable() -> None:
    def offline(texts: Any) -> list[list[float]]:
        raise OSError("model not cached and no network")

    result = knowledge_search.search_knowledge(
        _FakeConn(),  # type: ignore[arg-type]
        "corpus",
        CONFIG,
        offline,
        _rerank,
        "fictional guidance",
        2,
    )
    assert result.status == "unavailable"
    assert "failed to load" in result.message


def test_variant_metrics_score_each_search_variant_on_the_same_labels() -> None:
    labels = {"q1": ["a", "b"]}
    cases = [_case("q1", "first?")]
    searches = {
        "dense": lambda query, k: ["a", "b", "x", "y", "z"],
        "full_text": lambda query, k: ["x", "y", "z", "w", "v"],
    }

    metrics = retrieval.variant_metrics(labels, cases, searches)

    assert metrics == {
        "retrieval_precision_at_5_dense": pytest.approx(0.4),
        "retrieval_recall_at_5_dense": pytest.approx(1.0),
        "retrieval_precision_at_5_full_text": pytest.approx(0.0),
        "retrieval_recall_at_5_full_text": pytest.approx(0.0),
    }
    assert retrieval.variant_metrics({}, cases, searches) == {}


@pytest.mark.parametrize(
    ("variant", "expected"),
    [
        ("dense", ["c0", "c1"]),
        ("full_text", ["c3", "c1"]),
        ("hybrid", ["c1", "c0"]),
        ("hybrid_rerank", ["c1", "c3"]),
    ],
)
def test_ranked_ids_follow_each_variant(variant: str, expected: list[str]) -> None:
    conn: Any = _FakeConn()
    ids = knowledge_search.ranked_ids(
        conn, "corpus", CONFIG, _embed, _rerank, "fictional guidance", 2, variant
    )
    assert ids == expected


def test_the_full_variant_ranks_exactly_like_the_tool() -> None:
    conn: Any = _FakeConn()
    tool_ids = [row["chunk_id"] for row in _search(conn).data]
    assert tool_ids == knowledge_search.ranked_ids(
        conn,
        "corpus",
        CONFIG,
        _embed,
        _rerank,
        "fictional guidance",
        2,
        "hybrid_rerank",
    )


def _backend(tmp_path: Path) -> knowledge_backend.KnowledgeBackend:
    chunks_path = tmp_path / corpus.CHUNKS_FILENAME
    corpus.write_chunks(
        corpus.build_chunks(_raw_snapshot(tmp_path), {1234: "FICT"}, CUTOFF, CONFIG),
        chunks_path,
    )
    return knowledge_backend.KnowledgeBackend(chunks_path)


def test_warm_up_loads_both_models_in_the_background(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#99: each case starts its own tool server, and loading the models took
    about 10 s of the first search's time inside the case's wall-clock budget."""
    monkeypatch.chdir(ROOT)
    backend = _backend(tmp_path)
    loaded: list[str] = []

    def embed(texts: Any) -> list[list[float]]:
        loaded.append("embed")
        return [[1.0, 0.0] for _ in texts]

    def rerank(query: str, passages: Any) -> list[float]:
        loaded.append("rerank")
        return [0.0] * len(passages)

    backend.embed = embed
    backend.rerank = rerank

    backend.start_warm_up()
    backend.search("fictional guidance", 2)

    assert loaded[:2] == ["embed", "rerank"]


def test_a_search_waits_for_the_warm_up_instead_of_loading_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(ROOT)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    backend = _backend(tmp_path)
    finished: list[bool] = []

    def slow_embed(texts: Any) -> list[list[float]]:
        time.sleep(0.2)
        finished.append(True)
        return [[1.0, 0.0] for _ in texts]

    backend.embed = slow_embed
    backend.rerank = lambda query, passages: [0.0] * len(passages)
    backend.start_warm_up()
    backend.search("fictional guidance", 2)

    assert finished == [True]


def test_a_failed_warm_up_leaves_the_search_to_report_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(ROOT)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    backend = _backend(tmp_path)

    def missing(texts: Any) -> list[list[float]]:
        raise ImportError("No module named 'sentence_transformers'")

    backend.embed = missing
    backend.start_warm_up()
    assert backend.search("fictional guidance", 2).status == "unavailable"
