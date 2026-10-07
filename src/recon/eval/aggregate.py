"""Run-level aggregate metrics, computed from the per-case scores. See
`docs/contracts.md` §7 (`EvalRun.aggregate`).
"""

from recon.contracts import Case, CaseScore
from recon.eval import faithfulness
from recon.eval.gate import CASES_JUDGE_FAILED


def aggregate_scores(
    cases: list[Case], case_scores: list[CaseScore], agent_cost_eur: float
) -> dict[str, float]:
    """The run's aggregate metrics from its per-case scores. `agent_cost_eur` is
    the agents' share of the cost; the rest went to the judges."""
    n = len(case_scores)
    if n == 0:
        return {}
    total_cost_eur = sum(s.cost_eur for s in case_scores)

    applicable = [
        score
        for case, score in zip(cases, case_scores, strict=True)
        if case.expected_tool_path is not None
    ]

    aggregate: dict[str, float] = {
        "case_count": float(n),
        "task_completion_rate": sum(s.task_completion for s in case_scores) / n,
        "answer_score_mean": sum(s.answer_score for s in case_scores) / n,
        "tool_call_accuracy_mean": sum(s.tool_call_accuracy for s in case_scores) / n,
        "elapsed_ms_mean": sum(s.elapsed_ms for s in case_scores) / n,
        "tool_path_applicable_count": float(len(applicable)),
        # Which side of a case the credit went to, so cost decisions rest on
        # measurements (the judge model, say) rather than estimates.
        "agent_cost_eur": agent_cost_eur,
        "judge_cost_eur": total_cost_eur - agent_cost_eur,
    }
    if applicable:
        aggregate["tool_path_exact_rate"] = sum(
            s.tool_path_exact for s in applicable
        ) / len(applicable)
        aggregate["tool_path_equivalent_rate"] = sum(
            s.tool_path_equivalent for s in applicable
        ) / len(applicable)

    aggregate.update(_citation_aggregate(case_scores))
    aggregate.update(_failure_counts(case_scores))

    aggregate.update(_dimension_means(case_scores))
    return aggregate


def _citation_aggregate(case_scores: list[CaseScore]) -> dict[str, float]:
    """Means of the citation metrics over the cases that have them (ADR 0030)."""
    aggregate: dict[str, float] = {}
    for name in ("claim_support_rate", "citation_precision"):
        values = [v for s in case_scores if (v := getattr(s, name)) is not None]
        if values:
            aggregate[f"{name}_mean"] = sum(values) / len(values)
    scored = [s for s in case_scores if s.citation_precision is not None]
    if scored:
        aggregate["citation_scored_cases"] = float(len(scored))
    return aggregate


def _failure_counts(case_scores: list[CaseScore]) -> dict[str, float]:
    """Cases the judge couldn't score (#123), and cases per failure class over
    the cases that have one (#116)."""
    judge_failed = sum(s.judge_failed for s in case_scores)
    unjudged = {CASES_JUDGE_FAILED: float(judge_failed)} if judge_failed else {}
    # Not gated, so not an incomplete-run marker: faithfulness_mean just rests
    # on fewer cases.
    faithfulness_failed = sum(s.faithfulness_judge_failed for s in case_scores)
    if faithfulness_failed:
        unjudged["cases_faithfulness_judge_failed"] = float(faithfulness_failed)
    classified = [s.failure_class for s in case_scores if s.failure_class is not None]
    if not classified:
        return unjudged
    counts = {
        f"failure_{name}_count": float(classified.count(name))
        for name in sorted(set(classified))
    }
    return {**unjudged, **counts, "failure_classified_cases": float(len(classified))}


def _dimension_means(case_scores: list[CaseScore]) -> dict[str, float]:
    """Each rubric dimension's mean over the cases that scored it."""
    aggregate: dict[str, float] = {}
    dimension_names = {d for s in case_scores for d in s.rubric_scores}
    for dimension in dimension_names:
        values = [
            s.rubric_scores[dimension]
            for s in case_scores
            if dimension in s.rubric_scores
        ]
        aggregate[f"{dimension}_mean"] = sum(values) / len(values)
        if dimension == faithfulness.DIMENSION:
            # Scored only on cases that searched, so the mean may rest on few.
            aggregate["faithfulness_scored_cases"] = float(len(values))

    return aggregate
