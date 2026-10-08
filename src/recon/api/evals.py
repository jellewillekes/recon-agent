"""The evaluation endpoints (#117): `GET /evals`, `GET /evals/compare` and
`GET /evals/{run_id}`.

Read-only over the committed result files in `evals/results/`
(`RECON_EVAL_RESULTS_DIR`). They never start a run, so they never cost credit.
"""

import logging
import os
from pathlib import Path

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from recon.api.schemas import EvalList, EvalSummary
from recon.contracts import EvalRun
from recon.eval.comparison import Comparison, compare_runs
from recon.eval.gate import incomplete_reasons
from recon.eval.thresholds import load_thresholds

logger = logging.getLogger(__name__)

RESULTS_DIR = Path(os.environ.get("RECON_EVAL_RESULTS_DIR", "evals/results"))

router = APIRouter()


def _load_runs() -> dict[str, EvalRun]:
    """Every result file that parses, by run id. A file that doesn't is logged
    and left out, so one bad file doesn't take the list down."""
    runs: dict[str, EvalRun] = {}
    for path in sorted(RESULTS_DIR.glob("eval-*.json")):
        try:
            run = EvalRun.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as exc:
            logger.warning("skipping %s: it isn't a readable EvalRun (%s)", path, exc)
            continue
        runs[run.run_id] = run
    return runs


def _summary(run: EvalRun) -> EvalSummary:
    return EvalSummary(
        run_id=run.run_id,
        timestamp_utc=run.timestamp_utc,
        runtime=run.runtime,
        mode=run.mode,
        routing=run.routing,
        rubric_version=run.rubric_version,
        dataset=run.dataset,
        case_count=len(run.case_scores),
        task_completion_rate=run.aggregate.get("task_completion_rate"),
        answer_score_mean=run.aggregate.get("answer_score_mean"),
        total_cost_eur=run.total_cost_eur,
        cost_per_correct_answer_eur=run.aggregate.get("cost_per_correct_answer_eur"),
        incomplete=incomplete_reasons(run),
    )


def _no_such_eval(run_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": f"No eval run {run_id!r}. List eval runs at /evals."},
    )


@router.get("/evals")
async def evals() -> EvalList:
    """Eval runs, newest first."""
    runs = sorted(_load_runs().values(), key=lambda r: r.timestamp_utc, reverse=True)
    return EvalList(runs=[_summary(run) for run in runs])


@router.get(
    "/evals/compare", response_model=None, responses={200: {"model": Comparison}}
)
async def compare(baseline: str, candidate: str) -> Comparison | JSONResponse:
    """`candidate` judged against `baseline` by the gate. Runs it refuses come
    back with `comparable: false` and the reasons, not as an error."""
    runs = _load_runs()
    for run_id in (baseline, candidate):
        if run_id not in runs:
            return _no_such_eval(run_id)
    thresholds = load_thresholds()
    return compare_runs(
        runs[baseline],
        runs[candidate],
        thresholds.gate,
        correct_answer_score=thresholds.correct_answer_score,
        history=list(runs.values()),
    )


@router.get("/evals/{run_id}", response_model=None, responses={200: {"model": EvalRun}})
async def get_eval(run_id: str) -> EvalRun | JSONResponse:
    """One eval run with its per-case scores."""
    run = _load_runs().get(run_id)
    return _no_such_eval(run_id) if run is None else run
