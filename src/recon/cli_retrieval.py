"""`recon.cli retrieval propose`: draft relevance labels for the user (#18).

The labels in `evals/retrieval-labels.yaml` are the user's to set (AGENTS.md:
relevance labels aren't delegated). This only lists candidate chunks per case
so the user can pick the relevant ones. The candidates hold filing text, so
they go to the gitignored data/ folder, not into the repo.
"""

import argparse
from pathlib import Path
from typing import Any

import yaml

from recon.adapters import sec_edgar_tickers
from recon.adapters.finance_agent_bench import fetch_csv, load_cases
from recon.adapters.sec_edgar import DEFAULT_RAW_DIR
from recon.contracts import Case, EvalRun
from recon.eval import retrieval
from recon.eval.case_selection import read_case_file, select_cases
from recon.eval.faithfulness import Passages
from recon.tools.data_source import knowledge_chunks_path
from recon.tools.knowledge_backend import KnowledgeBackend

DEFAULT_CANDIDATES_PATH = Path("data/processed/retrieval-candidates.yaml")


def open_backend() -> KnowledgeBackend | None:
    """The filing-text index, or None (with a reason printed) when unavailable."""
    path = knowledge_chunks_path()
    if path is None:
        print("No filing-text corpus. Run `recon.cli edgar fetch-text` first.")
        return None
    backend = KnowledgeBackend(path)
    if backend.connection() is None:
        print(
            "The filing-text index isn't reachable, or isn't fully built. Set "
            "DATABASE_URL and run `recon.cli edgar index-text`."
        )
        return None
    return backend


def require_backend() -> KnowledgeBackend | None:
    """The filing-text index for an eval run, checked before credit is spent.

    None when the snapshot has no corpus, so the agent has no search tool.
    With a corpus the agent sees `search_knowledge`, so an index it can't reach
    would fail every search while the run still gets scored (#100).
    """
    path = knowledge_chunks_path()
    if path is None:
        return None
    backend = KnowledgeBackend(path)
    if backend.connection() is None:
        raise SystemExit(
            "The tool data has a filing-text corpus, but its index isn't reachable "
            "or isn't fully built, so every search_knowledge call would fail. Set "
            "DATABASE_URL to the index's Postgres (and run `recon.cli edgar "
            "index-text` if it isn't built), then run the eval again."
        )
    return backend


def with_retrieval_metrics(
    run: EvalRun, cases: list[Case], backend: KnowledgeBackend
) -> EvalRun:
    """Score the retriever on the run's labelled cases. Offline: no model call."""
    labels = retrieval.load_labels()
    if not any(case.case_id in labels for case in cases):
        return run
    ran = cases[: len(run.case_scores)]
    try:
        metrics = retrieval.retrieval_metrics(labels, ran, backend.chunk_ids)
        metrics |= retrieval.variant_metrics(
            labels,
            ran,
            {
                **{
                    variant: backend.variant_search(variant)
                    for variant in ("dense", "full_text", "hybrid")
                },
                "company": company_search(
                    backend,
                    ran,
                    case_companies(sorted(DEFAULT_RAW_DIR.glob("tickers*.txt"))),
                ),
            },
        )
    except RuntimeError as exc:
        print(f"Retrieval metrics skipped: a search failed ({exc})")
        return run
    return retrieval.with_retrieval_metrics(run, metrics)


def case_companies(paths: list[Path]) -> dict[str, str]:
    """Case id -> the one ticker the tickers files record for it (#104).

    A case recorded under two tickers compares companies, so it gets none.
    """
    tickers: dict[str, set[str]] = {}
    for ticker, case_ids in sec_edgar_tickers.read_case_ids(paths).items():
        for case_id in case_ids:
            tickers.setdefault(case_id, set()).add(ticker)
    return {
        case_id: names.pop() for case_id, names in tickers.items() if len(names) == 1
    }


def company_search(
    backend: KnowledgeBackend, cases: list[Case], companies: dict[str, str]
) -> retrieval.Search:
    """The tool's search with each case's company as `company_id`, as the agent
    is told to pass it. A case without one known searches unfiltered."""
    by_question = {case.question: companies.get(case.case_id) for case in cases}

    def search(query: str, top_k: int) -> list[str]:
        return backend.chunk_ids(query, top_k, by_question.get(query))

    return search


def replay_passages(backend: KnowledgeBackend) -> Passages:
    """Re-run a search for the faithfulness score (docs/adr/0026). A search
    that fails returns no passages, so that case goes unscored, with a note."""

    def passages(
        query: str, top_k: int, company_id: str | None
    ) -> list[dict[str, Any]]:
        result = backend.search(query, top_k, company_id)
        if result.status in ("unavailable", "invalid_input"):
            print(f"Faithfulness replay failed for {query!r}: {result.message}")
            return []
        return result.data

    return passages


def _cmd_propose(args: argparse.Namespace) -> None:
    backend = open_backend()
    if backend is None:
        raise SystemExit(1)
    cases = select_cases(load_cases(fetch_csv(args.path)), read_case_file(args.cases))
    drafts = {}
    for case in cases[: args.count]:
        result = backend.search(case.question, args.per_case)
        if result.status != "ok":
            print(f"{case.case_id}: {result.message}")
        drafts[case.case_id] = {
            "question": case.question,
            "candidates": [
                {
                    key: row[key]
                    for key in ("chunk_id", "company_id", "form", "filed", "section")
                }
                | {"text": row["text"][:600]}
                for row in result.data
            ],
        }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(yaml.safe_dump(drafts, sort_keys=False, allow_unicode=True))
    print(
        f"Wrote candidates for {len(drafts)} case(s) to {args.out}. Copy the ids of "
        f"the relevant chunks into {retrieval.DEFAULT_LABELS_PATH} as "
        "`<case_id>: [<chunk_id>, ...]`."
    )


def add_retrieval_parser(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
    dataset_path: Path,
) -> None:
    """Register `retrieval propose` on the top-level parser."""
    parser = subparsers.add_parser("retrieval", help="Retrieval labels (#18).")
    sub = parser.add_subparsers(dest="retrieval_command", required=True)
    propose = sub.add_parser("propose", help="Draft candidate chunks per case.")
    propose.add_argument("--cases", type=Path, default=Path("evals/smoke-cases.txt"))
    propose.add_argument("--count", type=int, default=5, help="Cases to draft.")
    propose.add_argument("--per-case", type=int, default=10, help="Candidates each.")
    propose.add_argument("--out", type=Path, default=DEFAULT_CANDIDATES_PATH)
    propose.add_argument("--path", type=Path, default=dataset_path)
    propose.set_defaults(func=_cmd_propose)
