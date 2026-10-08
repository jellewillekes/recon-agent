"""Score one case: run the agent, judge its answer, build its `CaseScore`.
See `docs/contracts.md` §7. `harness.py` runs this over a whole case set.
"""

from dataclasses import dataclass
from pathlib import Path

import yaml

from recon.contracts import AgentResult, Case, CaseScore
from recon.eval import claim_replay, faithfulness, metrics, trajectory
from recon.eval.judge import DEFAULT_MODELS_CONFIG_PATH, JudgeResult, judge_case
from recon.eval.judge_failures import (
    JUDGE_ERRORS,
    SessionLimitReached,
    error_is_session_limit,
    is_session_limit,
)
from recon.eval.rubrics import Rubric
from recon.runtimes.base import Runtime
from recon.tracing import record_agent_result, record_judge_call, span

JUDGED_DESPITE_ERROR_NOTE = "answer judged despite runtime error"


def _investigator_model(models_config_path: Path) -> str | None:
    config = yaml.safe_load(models_config_path.read_text(encoding="utf-8"))
    model = config.get("investigator", {}).get("model")
    return str(model) if model else None


def score_case(
    case: Case,
    runtime: Runtime,
    rubrics: dict[str, Rubric],
    *,
    models_config_path: Path = DEFAULT_MODELS_CONFIG_PATH,
    passages: faithfulness.Passages | None = None,
    retrieval_labels: dict[str, list[str]] | None = None,
    correct_answer_score: float | None = None,
    facts: claim_replay.Facts | None = None,
) -> tuple[CaseScore, AgentResult]:
    """Run, judge and score one case, under an `eval.case` trace span.

    With `passages`, an answer that used filing-text search is also scored
    for faithfulness to what the search returned, and on cases in
    `retrieval_labels` its retrieval is scored too. `correct_answer_score` is
    the cutoff the failure class judges the answer by (#116). With `facts`,
    the answer's claims are verified against its replayed fact calls (#139).

    Raises `SessionLimitReached`, carrying the scored case, when the case ran
    into the subscription's session limit (#123)."""
    with span("eval.case", **{"recon.case_id": case.case_id}) as case_span:
        score, agent_result, session_limit = _score_case(
            case,
            runtime,
            rubrics,
            models_config_path=models_config_path,
            passages=passages,
            facts=facts,
        )
        score = _with_diagnosis(
            score,
            case,
            agent_result,
            passages=passages,
            relevant=(retrieval_labels or {}).get(case.case_id),
            correct_answer_score=correct_answer_score,
        )
        case_span.set_attributes(
            {
                "recon.answer_score": score.answer_score,
                "recon.task_completion": score.task_completion,
                "recon.cost_eur": score.cost_eur,
            }
        )
    if session_limit:
        raise SessionLimitReached(score, agent_result)
    return score, agent_result


@dataclass(frozen=True)
class _Judged:
    """What judging one answer produced. `failed` means the rubric judge call
    failed, so the scores are placeholders (#123)."""

    rubric_scores: dict[str, float]
    answer_score: float
    cost_eur: float = 0.0
    failed: bool = False
    faithfulness_failed: bool = False
    session_limit: bool = False


def _score_case(
    case: Case,
    runtime: Runtime,
    rubrics: dict[str, Rubric],
    *,
    models_config_path: Path,
    passages: faithfulness.Passages | None,
    facts: claim_replay.Facts | None,
) -> tuple[CaseScore, AgentResult, bool]:
    """Score one case. The bool says whether it ran into the session limit."""
    with span("invoke_agent") as agent_span:
        agent_result = runtime.run(case)
        # Multi mode picks a model per role (config/roles.yaml), so only a
        # single-mode run has one model to name.
        model = (
            _investigator_model(models_config_path)
            if agent_result.mode == "single"
            else None
        )
        record_agent_result(agent_span, agent_result, model)

    exact, exact_note = metrics.tool_path_exact(case, agent_result)
    equivalent, _ = metrics.tool_path_equivalent(case, agent_result)

    has_answer = bool(agent_result.answer.strip())
    notes_parts: list[str] = []
    if agent_result.error is not None:
        notes_parts.append(f"runtime error: {agent_result.error}")
        if has_answer:
            notes_parts.append(JUDGED_DESPITE_ERROR_NOTE)
    if exact_note is not None:
        notes_parts.append(exact_note)

    # Gate on the answer, not on `error`: runtimes also set `error` for
    # non-fatal outcomes that keep a complete answer (a token budget only
    # checked after the run finished, a LangGraph run paused before its
    # review-flag write). A real failure always returns an empty answer.
    # See docs/adr/0013.
    judged = (
        _judge_answer(
            case,
            agent_result,
            rubrics,
            models_config_path=models_config_path,
            passages=passages,
            notes_parts=notes_parts,
        )
        if has_answer
        else _Judged(
            rubric_scores={dimension: 0.0 for dimension in rubrics}, answer_score=0.0
        )
    )

    score = CaseScore(
        case_id=case.case_id,
        task_completion=metrics.task_completion(agent_result),
        answer_score=judged.answer_score,
        tool_path_exact=exact,
        tool_path_equivalent=equivalent,
        tool_call_accuracy=metrics.tool_call_accuracy(agent_result),
        rubric_scores=judged.rubric_scores,
        cost_eur=agent_result.cost_eur + judged.cost_eur,
        elapsed_ms=agent_result.elapsed_ms,
        notes="; ".join(notes_parts),
        tool_names=[call.tool for call in agent_result.tool_calls],
        claim_support_rate=metrics.claim_support_rate(agent_result),
        citation_precision=metrics.citation_precision(agent_result),
        judge_failed=judged.failed,
        faithfulness_judge_failed=judged.faithfulness_failed,
        verifications=(
            claim_replay.verify_answer(agent_result, facts)
            if facts is not None
            else None
        ),
    )
    session_limit = judged.session_limit or is_session_limit(agent_result.error)
    return score, agent_result, session_limit


def _with_diagnosis(
    score: CaseScore,
    case: Case,
    agent_result: AgentResult,
    *,
    passages: faithfulness.Passages | None,
    relevant: list[str] | None,
    correct_answer_score: float | None,
) -> CaseScore:
    """`score` with its run-path breakdown and failure class (#116)."""
    path = trajectory.trajectory_score(
        case, agent_result, score.rubric_scores, passages=passages, relevant=relevant
    )
    failure_class, reason = trajectory.classify_failure(
        agent_result,
        path,
        answer_score=score.answer_score,
        judge_failed=score.judge_failed,
        correct_answer_score=correct_answer_score,
    )
    return score.model_copy(
        update={
            "trajectory": path,
            "failure_class": failure_class,
            "failure_reason": reason,
        }
    )


def _judge_answer(
    case: Case,
    agent_result: AgentResult,
    rubrics: dict[str, Rubric],
    *,
    models_config_path: Path,
    passages: faithfulness.Passages | None,
    notes_parts: list[str],
) -> _Judged:
    """Run the rubric judge, then the faithfulness judge when `passages` is
    given. A failed call is noted in `notes_parts` instead of raised (#123)."""
    try:
        judge_result = _rubric_judge(case, agent_result, rubrics, models_config_path)
    except JUDGE_ERRORS as exc:
        notes_parts.append(f"judge failed: {exc}")
        return _Judged(
            rubric_scores={},
            answer_score=0.0,
            failed=True,
            session_limit=error_is_session_limit(exc),
        )

    rubric_scores = dict(judge_result.rubric_scores)
    faithful, faithful_cost, error = (
        _faithfulness_or_note(
            case, agent_result, passages, models_config_path, notes_parts
        )
        if passages is not None
        else (None, 0.0, None)
    )
    if faithful is not None:
        # Not a rubric, so weighted_answer_score ignores it.
        rubric_scores[faithfulness.DIMENSION] = faithful
    return _Judged(
        rubric_scores=rubric_scores,
        answer_score=metrics.weighted_answer_score(judge_result.rubric_scores, rubrics),
        cost_eur=judge_result.cost_eur + faithful_cost,
        faithfulness_failed=error is not None,
        session_limit=error is not None and error_is_session_limit(error),
    )


def _rubric_judge(
    case: Case,
    agent_result: AgentResult,
    rubrics: dict[str, Rubric],
    models_config_path: Path,
) -> JudgeResult:
    """`judge_case` under its own judge trace span."""
    with span("chat judge") as judge_span:
        result = judge_case(
            case, agent_result, rubrics, models_config_path=models_config_path
        )
        record_judge_call(
            judge_span,
            model=result.model,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            num_turns=result.num_turns,
            cost_eur=result.cost_eur,
        )
    return result


def _faithfulness_or_note(
    case: Case,
    agent_result: AgentResult,
    passages: faithfulness.Passages,
    models_config_path: Path,
    notes_parts: list[str],
) -> tuple[float | None, float, Exception | None]:
    """The faithfulness score, its cost, and the error when the call failed.
    A failed call is noted, not raised (#123)."""
    try:
        result = _judge_faithfulness(case, agent_result, passages, models_config_path)
    except JUDGE_ERRORS as exc:
        notes_parts.append(f"faithfulness judge failed: {exc}")
        return None, 0.0, exc
    return result.score, result.cost_eur, None


def _judge_faithfulness(
    case: Case,
    agent_result: AgentResult,
    passages: faithfulness.Passages,
    models_config_path: Path,
) -> faithfulness.FaithfulnessResult:
    """`faithfulness.judge_faithfulness` under its own judge trace span."""
    with span("chat judge faithfulness") as judge_span:
        result = faithfulness.judge_faithfulness(
            case, agent_result, passages, models_config_path=models_config_path
        )
        record_judge_call(
            judge_span,
            model=result.model,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            num_turns=result.num_turns,
            cost_eur=result.cost_eur,
        )
    return result
