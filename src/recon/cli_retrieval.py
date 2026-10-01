"""`recon.cli retrieval propose`: draft relevance labels for the user (#18).

The labels in `evals/retrieval-labels.yaml` are the user's to set (AGENTS.md:
relevance labels aren't delegated). This only lists candidate chunks per case
so the user can pick the relevant ones. The candidates hold filing text, so
they go to the gitignored data/ folder, not into the repo.
"""

import argparse
from pathlib import Path

import yaml

from recon.adapters.finance_agent_bench import fetch_csv, load_cases
from recon.eval import retrieval
from recon.eval.case_selection import read_case_file, select_cases
from recon.tools.data_source import knowledge_chunks_path
from recon.tools.knowledge_search import KnowledgeBackend

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
            "The filing-text index isn't reachable or built. Set DATABASE_URL and "
            "run `recon.cli edgar index-text`."
        )
        return None
    return backend


def _cmd_propose(args: argparse.Namespace) -> None:
    backend = open_backend()
    if backend is None:
        raise SystemExit(1)
    cases = select_cases(load_cases(fetch_csv(args.path)), read_case_file(args.cases))
    drafts = {}
    for case in cases[: args.count]:
        result = backend.search(case.question, args.per_case)
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
