"""Retrieval metrics for `search_knowledge` (step 13, #18).

Scores the retriever, not the agent: each labelled case's question goes to
the search as-is, and the top results are compared with the chunks the user
marked relevant in `evals/retrieval-labels.yaml`. No model is called, so it
costs no credit. The labels are the user's (AGENTS.md: relevance labels are
not delegated); `recon.cli retrieval propose` only drafts candidates.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from recon.contracts import Case, EvalRun

DEFAULT_LABELS_PATH = Path("evals/retrieval-labels.yaml")
K = 5

# (query, top_k) -> chunk ids, best first.
Search = Callable[[str, int], list[str]]


def load_labels(path: Path = DEFAULT_LABELS_PATH) -> dict[str, list[str]]:
    """case_id -> relevant chunk ids. Empty when the file doesn't exist yet."""
    if not path.exists():
        return {}
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {case_id: list(ids or []) for case_id, ids in raw.items()}


def precision_at_k(retrieved: list[str], relevant: set[str], k: int = K) -> float:
    """Share of the top `k` results that are relevant."""
    top = retrieved[:k]
    return sum(1 for chunk in top if chunk in relevant) / k


def recall_at_k(retrieved: list[str], relevant: set[str], k: int = K) -> float:
    """Share of the relevant chunks found in the top `k`. 1.0 with none labelled."""
    if not relevant:
        return 1.0
    return len(relevant & set(retrieved[:k])) / len(relevant)


def retrieval_metrics(
    labels: dict[str, list[str]], cases: list[Case], search: Search
) -> dict[str, float]:
    """Mean precision@5 and recall@5 over the run's labelled cases. Empty if none."""
    labelled = [case for case in cases if case.case_id in labels]
    if not labelled:
        return {}
    precisions, recalls = [], []
    for case in labelled:
        retrieved = search(case.question, K)
        relevant = set(labels[case.case_id])
        precisions.append(precision_at_k(retrieved, relevant))
        recalls.append(recall_at_k(retrieved, relevant))
    return {
        "retrieval_labelled_cases": float(len(labelled)),
        "retrieval_precision_at_5": sum(precisions) / len(labelled),
        "retrieval_recall_at_5": sum(recalls) / len(labelled),
    }


def with_retrieval_metrics(run: EvalRun, metrics: dict[str, float]) -> EvalRun:
    """`run` with the retrieval metrics added to its aggregate."""
    if not metrics:
        return run
    return run.model_copy(update={"aggregate": {**run.aggregate, **metrics}})
