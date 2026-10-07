"""Retrieval metrics for `search_knowledge` (step 13, #18).

Scores the retriever, not the agent: each labelled case's question goes to
the search as-is, and the top results are compared with the chunks the user
marked relevant in `evals/retrieval-labels.yaml`. No model is called, so it
costs no credit. The labels are the user's (AGENTS.md: relevance labels are
not delegated); `recon.cli retrieval propose` only drafts candidates.
"""

import hashlib
import math
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


def labels_hash(path: Path = DEFAULT_LABELS_PATH) -> str:
    """Content hash of the labels file, so retrieval metrics are only compared
    between runs scored on the same labels (#105)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def precision_at_k(retrieved: list[str], relevant: set[str], k: int = K) -> float:
    """Share of the top `k` results that are relevant."""
    top = retrieved[:k]
    return sum(1 for chunk in top if chunk in relevant) / k


def recall_at_k(retrieved: list[str], relevant: set[str], k: int = K) -> float:
    """Share of the relevant chunks found in the top `k`. 1.0 with none labelled."""
    if not relevant:
        return 1.0
    return len(relevant & set(retrieved[:k])) / len(relevant)


def reciprocal_rank_at_k(retrieved: list[str], relevant: set[str], k: int = K) -> float:
    """1 / the rank of the first relevant result in the top `k`, else 0. Its
    mean over the labelled cases is MRR@k (#116)."""
    for rank, chunk in enumerate(retrieved[:k], start=1):
        if chunk in relevant:
            return 1 / rank
    return 0.0


def ndcg_at_k(retrieved: list[str], relevant: set[str], k: int = K) -> float:
    """Normalised discounted cumulative gain at `k`, with binary relevance:
    how close the top `k` come to listing the relevant chunks first (#116).
    1.0 with none labelled, like `recall_at_k`."""
    if not relevant:
        return 1.0
    dcg = sum(
        1 / math.log2(rank + 1)
        for rank, chunk in enumerate(retrieved[:k], start=1)
        if chunk in relevant
    )
    ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(len(relevant), k) + 1))
    return dcg / ideal


def retrieval_metrics(
    labels: dict[str, list[str]], cases: list[Case], search: Search
) -> dict[str, float]:
    """Mean precision@5, recall@5, MRR@5 and NDCG@5 over the run's labelled
    cases. Empty if none."""
    labelled = [case for case in cases if case.case_id in labels]
    if not labelled:
        return {}
    scorers = {
        "retrieval_precision_at_5": precision_at_k,
        "retrieval_recall_at_5": recall_at_k,
        "retrieval_mrr_at_5": reciprocal_rank_at_k,
        "retrieval_ndcg_at_5": ndcg_at_k,
    }
    totals = dict.fromkeys(scorers, 0.0)
    for case in labelled:
        retrieved = search(case.question, K)
        relevant = set(labels[case.case_id])
        for name, scorer in scorers.items():
            totals[name] += scorer(retrieved, relevant, K)
    return {
        "retrieval_labelled_cases": float(len(labelled)),
        **{name: total / len(labelled) for name, total in totals.items()},
    }


def with_retrieval_metrics(
    run: EvalRun, metrics: dict[str, float], labels_path: Path = DEFAULT_LABELS_PATH
) -> EvalRun:
    """`run` with the retrieval metrics added to its aggregate, and the hash of
    the labels that scored them."""
    if not metrics:
        return run
    return run.model_copy(
        update={
            "aggregate": {**run.aggregate, **metrics},
            "retrieval_labels_hash": labels_hash(labels_path),
        }
    )


def variant_metrics(
    labels: dict[str, list[str]], cases: list[Case], searches: dict[str, Search]
) -> dict[str, float]:
    """Precision@5 and recall@5 per search variant, on the same labelled cases.

    Shows what each part of the hybrid search adds. The tool's own ranking is
    scored by `retrieval_metrics`; this covers the variants it's built from.
    """
    metrics: dict[str, float] = {}
    for name, search in searches.items():
        scored = retrieval_metrics(labels, cases, search)
        if not scored:
            return {}
        metrics[f"retrieval_precision_at_5_{name}"] = scored["retrieval_precision_at_5"]
        metrics[f"retrieval_recall_at_5_{name}"] = scored["retrieval_recall_at_5"]
    return metrics
