"""Pure, no-LLM per-case metrics: task completion, tool-path match, tool-call
accuracy. See `docs/contracts.md` §7 (`CaseScore`) and `docs/data-sources.md`'s
"Finding" section for why `tool_path_exact`/`tool_path_equivalent` need an
explicit not-applicable case.
"""

from recon.contracts import AgentResult, Case

TOOL_PATH_NA_NOTE = "N/A: no expected_tool_path for this case"


def task_completion(agent_result: AgentResult) -> bool:
    """A run "completed" if it produced a non-empty answer without erroring."""
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
    is not an error, it can be the correct finding. `invalid_input`,
    `unavailable`, and the parser's own `unknown` fallback count against it.
    A case with no tool calls scores 1.0: nothing to penalize here, since not
    every question needs a lookup (`prompts/investigator.md`).
    """
    if not agent_result.tool_calls:
        return 1.0
    usable_statuses = {"ok", "truncated", "empty"}
    usable = sum(
        1 for call in agent_result.tool_calls if call.status in usable_statuses
    )
    return usable / len(agent_result.tool_calls)
