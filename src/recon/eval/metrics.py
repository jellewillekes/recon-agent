"""Pure, no-LLM per-case metrics: task completion, tool-path match, tool-call
accuracy. See `docs/contracts.md` §7 (`CaseScore`) and `docs/data-sources.md`'s
"Finding" section for why `tool_path_exact`/`tool_path_equivalent` need an
explicit not-applicable case.
"""

from recon.contracts import AgentResult, Case, CaseScore
from recon.eval.rubrics import Rubric

TOOL_PATH_NA_NOTE = "N/A: no expected_tool_path for this case"

# `ToolCall.status` mirrors whichever result the tool returned: `ToolResult`
# for reads, `ReviewFlagResult` for `flag_case_for_review`. Listed explicitly
# rather than derived from the contracts' Literals, so a status added there
# later has to be classified here on purpose instead of counting as usable
# by default.
USABLE_READ_STATUSES = frozenset({"ok", "truncated", "empty"})
USABLE_WRITE_STATUSES = frozenset(
    {"would_write", "confirmation_required", "created", "already_exists"}
)
USABLE_STATUSES = USABLE_READ_STATUSES | USABLE_WRITE_STATUSES


def task_completion(agent_result: AgentResult) -> bool:
    """A run "completed" if it produced a non-empty answer without erroring.

    Deliberately stricter than the harness's judge gate, which grades any
    non-empty answer. A run that breached its budget or paused for review
    didn't finish within its constraints, even when its answer is still
    worth grading. See docs/adr/0013.
    """
    return agent_result.error is None and bool(agent_result.answer.strip())


def tool_path_exact(case: Case, agent_result: AgentResult) -> tuple[bool, str | None]:
    """Tool calls, in order, exactly match `expected_tool_path`.

    finance-agent-bench never populates `expected_tool_path` (every case's is
    `None`) — that's "not applicable," not a miss, so it defaults to `True`
    with an explanatory note rather than scoring absence as a failure.
    """
    if case.expected_tool_path is None:
        return True, TOOL_PATH_NA_NOTE
    actual = [call.tool for call in agent_result.tool_calls]
    return actual == case.expected_tool_path, None


def tool_path_equivalent(
    case: Case, agent_result: AgentResult
) -> tuple[bool, str | None]:
    """Same set of tools used, any order — "different path, same evidence"."""
    if case.expected_tool_path is None:
        return True, TOOL_PATH_NA_NOTE
    actual = {call.tool for call in agent_result.tool_calls}
    return actual == set(case.expected_tool_path), None


def tool_call_accuracy(agent_result: AgentResult) -> float:
    """Fraction of tool calls that returned a usable result.

    `empty` counts as usable — `docs/contracts.md` §3 is explicit that empty
    is not an error, it can be the correct finding. All four
    `flag_case_for_review` statuses count as usable too: each is a normal
    step of the write protocol (§6), not a failure. A failed write raises
    instead of returning a status, so it still lands as `unknown`.
    `invalid_input`, `unavailable`, and the parser's own `unknown` fallback
    count against it. A case with no tool calls scores 1.0: nothing to
    penalize here, since not every question needs a lookup
    (`prompts/investigator.md`).
    """
    if not agent_result.tool_calls:
        return 1.0
    usable = sum(
        1 for call in agent_result.tool_calls if call.status in USABLE_STATUSES
    )
    return usable / len(agent_result.tool_calls)


def _verified_refs(agent_result: AgentResult) -> set[str]:
    return {item.ref for item in agent_result.evidence_items if item.verified}


def claim_support_rate(agent_result: AgentResult) -> float | None:
    """Share of key claims with at least one verified ref (ADR 0030).

    None without key claims: an older result, a runtime that doesn't produce
    claims, or an answer with nothing to claim has nothing to score.
    """
    key = [claim for claim in agent_result.claims if claim.importance == "key"]
    if not key:
        return None
    verified = _verified_refs(agent_result)
    supported = sum(1 for claim in key if verified & set(claim.evidence_refs))
    return supported / len(key)


def citation_precision(agent_result: AgentResult) -> float | None:
    """Share of the distinct refs the claims cite that a tool returned in the
    run (ADR 0030). None when nothing is cited."""
    cited = {ref for claim in agent_result.claims for ref in claim.evidence_refs}
    if not cited:
        return None
    return len(cited & _verified_refs(agent_result)) / len(cited)


def weighted_answer_score(
    rubric_scores: dict[str, float], rubrics: dict[str, Rubric]
) -> float:
    """Weighted mean of the per-dimension scores, by each rubric's `weight`.

    This is the "weighted answer_score" `docs/contracts.md` §9 gates on.
    Only dimensions that were actually scored count, and their weights are
    renormalized. The judge omits a dimension when a case yields no
    assertions for it, and that should not count as a zero. Returns 0.0 when
    nothing scored carries weight. See docs/adr/0014.
    """
    weighted = [
        (rubrics[dimension].weight, score)
        for dimension, score in rubric_scores.items()
        if dimension in rubrics
    ]
    total_weight = sum(weight for weight, _ in weighted)
    if total_weight <= 0:
        return 0.0
    return sum(weight * score for weight, score in weighted) / total_weight


def cost_per_correct_answer_eur(
    case_scores: list[CaseScore], correct_answer_score: float
) -> float | None:
    """Total cost divided by the cases whose answer_score reaches the cutoff.

    None when no case is correct: there's no finite cost per correct answer.
    The cutoff comes from config/thresholds.yaml and is the user's (#77).
    """
    correct = sum(1 for s in case_scores if s.answer_score >= correct_answer_score)
    if correct == 0:
        return None
    return sum(s.cost_eur for s in case_scores) / correct
