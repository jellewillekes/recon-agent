"""The run path of one case, scored step by step, and the main reason it
failed (#116). Computed from the recorded trace and the scores the judges
already gave, so no model is called and it costs nothing.

`AgentResult` keeps each tool call's arguments and status but not its rows
(docs/contracts.md §4), so retrieval quality replays the agent's own
filing-text searches, as faithfulness does (ADR 0026). The failure classes
and the order they're checked in are ADR 0031.
"""

import json
from collections.abc import Collection

from recon.contracts import (
    AgentResult,
    Case,
    FailureClass,
    RetrievalQuality,
    TrajectoryScore,
)
from recon.eval import faithfulness, metrics, retrieval

GROUNDING_DIMENSION = "evidence_grounding"
CORRECTNESS_DIMENSION = "answer_correctness"

# Both runtimes word a budget breach as "<kind> budget of <limit> exceeded".
_BUDGET_MARKERS = ("budget of", "exceeded")


def trajectory_score(
    case: Case,
    agent_result: AgentResult,
    rubric_scores: dict[str, float],
    *,
    passages: faithfulness.Passages | None,
    relevant: Collection[str] | None,
) -> TrajectoryScore:
    """Score each step of `agent_result`'s run path. `relevant` is the case's
    labelled chunk ids; with `passages`, the agent's searches are replayed to
    score its retrieval against them."""
    calls = agent_result.tool_calls
    return TrajectoryScore(
        tool_selection=_tool_selection(case, agent_result),
        argument_correctness=(
            sum(1 for call in calls if call.status != "invalid_input") / len(calls)
            if calls
            else None
        ),
        retrieval_quality=(
            _retrieval_quality(agent_result, passages, set(relevant))
            if passages is not None and relevant
            else None
        ),
        evidence_sufficiency=metrics.claim_support_rate(agent_result),
        recovery=_recovery(agent_result),
        efficiency=_efficiency(agent_result),
        grounding=rubric_scores.get(GROUNDING_DIMENSION),
        final_correctness=rubric_scores.get(CORRECTNESS_DIMENSION),
    )


def _tool_selection(case: Case, agent_result: AgentResult) -> float | None:
    """Overlap (Jaccard) of the tools used with the expected ones."""
    if case.expected_tool_path is None:
        return None
    used = {call.tool for call in agent_result.tool_calls}
    expected = set(case.expected_tool_path)
    union = used | expected
    return len(used & expected) / len(union) if union else 1.0


def _retrieval_quality(
    agent_result: AgentResult, passages: faithfulness.Passages, relevant: set[str]
) -> RetrievalQuality:
    retrieved = [
        passage["chunk_id"] for passage in faithfulness.replay(agent_result, passages)
    ]
    return RetrievalQuality(
        recall=retrieval.recall_at_k(retrieved, relevant, k=len(retrieved)),
        precision_at_5=retrieval.precision_at_k(retrieved, relevant),
        mrr_at_5=retrieval.reciprocal_rank_at_k(retrieved, relevant),
        ndcg_at_5=retrieval.ndcg_at_k(retrieved, relevant),
        retrieved_count=len(retrieved),
    )


def _failed_indexes(agent_result: AgentResult) -> list[int]:
    return [
        index
        for index, call in enumerate(agent_result.tool_calls)
        if call.status not in metrics.USABLE_STATUSES
    ]


def _recovery(agent_result: AgentResult) -> float | None:
    """Share of failed calls followed by a usable call to the same tool."""
    calls = agent_result.tool_calls
    failed = _failed_indexes(agent_result)
    if not failed:
        return None
    recovered = sum(
        1
        for index in failed
        if any(
            later.tool == calls[index].tool and later.status in metrics.USABLE_STATUSES
            for later in calls[index + 1 :]
        )
    )
    return recovered / len(failed)


def _efficiency(agent_result: AgentResult) -> float | None:
    """Share of calls that weren't an exact repeat of an earlier call."""
    calls = agent_result.tool_calls
    if not calls:
        return None
    distinct = {
        (call.tool, json.dumps(call.arguments, sort_keys=True, default=str))
        for call in calls
    }
    return len(distinct) / len(calls)


def classify_failure(
    agent_result: AgentResult,
    trajectory: TrajectoryScore,
    *,
    answer_score: float,
    judge_failed: bool,
    correct_answer_score: float | None,
) -> tuple[FailureClass | None, str | None]:
    """The main reason a case failed and one line on what that rests on.
    "none" when the answer reached the cutoff. No class when the case wasn't
    scored. The checks run in ADR 0031's order: the first that applies wins."""
    error = agent_result.error
    if error and all(marker in error for marker in _BUDGET_MARKERS):
        return "budget", error
    if error and not agent_result.answer.strip():
        return "runtime_error", error
    if judge_failed:
        return None, "the judge couldn't score this case"
    if correct_answer_score is None:
        return None, "no correct_answer_score cutoff is set"
    if answer_score >= correct_answer_score:
        return "none", None
    return _wrong_answer_class(
        agent_result, trajectory, answer_score, correct_answer_score
    )


def _wrong_answer_class(
    agent_result: AgentResult,
    trajectory: TrajectoryScore,
    answer_score: float,
    cutoff: float,
) -> tuple[FailureClass, str]:
    calls = agent_result.tool_calls
    if trajectory.recovery is not None and trajectory.recovery < 1.0:
        unrecovered = round(
            len(_failed_indexes(agent_result)) * (1 - trajectory.recovery)
        )
        return "tool_use", f"{unrecovered} failed tool call(s) never succeeded on retry"
    if not calls:
        return "tool_use", "answered without calling a tool"
    quality = trajectory.retrieval_quality
    if quality is not None and quality.recall == 0.0:
        return "retrieval", "none of the labelled passages were retrieved"
    if all(call.status == "empty" for call in calls):
        return "retrieval", "every lookup came back empty"
    return (
        "reasoning",
        (
            f"the tools returned data, but the answer scored {answer_score:.2f}, "
            f"below {cutoff}"
        ),
    )
