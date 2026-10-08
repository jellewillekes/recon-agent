"""Tests for the evaluation endpoints (`api/evals.py`, #117). They read
result files from a temporary directory and never start a run."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient, Response

from recon.api import evals as api_evals
from recon.api import main as api_main
from recon.contracts import CaseScore, ClaimVerification, EvalRun

pytestmark = [pytest.mark.unit, pytest.mark.anyio]

T0 = datetime(2026, 10, 7, 12, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _run(run_id: str, **overrides: object) -> EvalRun:
    defaults: dict[str, object] = {
        "run_id": run_id,
        "timestamp_utc": T0,
        "dataset": "finance-agent-bench@pin",
        "dataset_license": "MIT",
        "dataset_attribution": "attribution",
        "runtime": "agent_sdk",
        "mode": "multi",
        "model_config_hash": "abc",
        "prompt_hashes": {"supervisor": "abc"},
        "rubric_version": "4",
        "case_scores": [
            CaseScore(
                case_id="a",
                task_completion=True,
                answer_score=0.6,
                tool_path_exact=True,
                tool_path_equivalent=True,
                tool_call_accuracy=1.0,
                rubric_scores={},
                cost_eur=0.2,
                elapsed_ms=1000,
                notes="",
            )
        ],
        "aggregate": {
            "task_completion_rate": 0.9,
            "answer_score_mean": 0.6,
            "cost_per_correct_answer_eur": 0.25,
        },
        "total_cost_eur": 1.5,
        "tool_data_snapshot": "snap",
    }
    defaults.update(overrides)
    return EvalRun(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(api_evals, "RESULTS_DIR", tmp_path)
    for run in (
        _run("eval-off"),
        _run("eval-on", routing=True, timestamp_utc=T0 + timedelta(hours=1)),
        _run("eval-old", rubric_version="3", timestamp_utc=T0 - timedelta(days=1)),
    ):
        (tmp_path / f"{run.run_id}.json").write_text(run.model_dump_json())
    return tmp_path


async def _get(path: str, **params: str) -> Response:
    transport = ASGITransport(app=api_main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, params=params)


async def test_evals_lists_runs_newest_first(results: Path) -> None:
    (results / "eval-broken.json").write_text("{not json")

    resp = await _get("/evals")

    assert resp.status_code == 200
    runs = resp.json()["runs"]
    assert [r["run_id"] for r in runs] == ["eval-on", "eval-off", "eval-old"]
    assert runs[0]["routing"] is True and runs[0]["case_count"] == 1
    # The summary reads each figure from the right aggregate key (#122 review).
    assert runs[0]["task_completion_rate"] == 0.9
    assert runs[0]["answer_score_mean"] == 0.6
    assert runs[0]["total_cost_eur"] == 1.5
    assert runs[0]["cost_per_correct_answer_eur"] == 0.25
    assert runs[0]["incomplete"] == []


async def test_a_run_that_didnt_score_every_case_says_so_in_the_list(
    results: Path,
) -> None:
    """Review: its answer_score_mean includes placeholder zeros."""
    run = _run(
        "eval-partial",
        timestamp_utc=T0 + timedelta(hours=2),
        aggregate={"answer_score_mean": 0.3, "cases_judge_failed": 1.0},
    )
    (results / "eval-partial.json").write_text(run.model_dump_json())

    resp = await _get("/evals")

    partial = resp.json()["runs"][0]
    assert partial["run_id"] == "eval-partial"
    assert partial["incomplete"] == ["has 1 case(s) the judge couldn't score"]


async def test_an_eval_run_comes_back_with_its_case_scores(results: Path) -> None:
    resp = await _get("/evals/eval-off")

    assert resp.status_code == 200
    assert resp.json()["case_scores"][0]["case_id"] == "a"


async def test_an_unknown_eval_run_is_a_404_that_points_at_the_list(
    results: Path,
) -> None:
    resp = await _get("/evals/eval-nope")

    assert resp.status_code == 404
    assert "/evals" in resp.json()["detail"]


async def test_compare_judges_comparable_runs(results: Path) -> None:
    resp = await _get("/evals/compare", baseline="eval-off", candidate="eval-on")

    body = resp.json()
    assert body["comparable"] is True
    verdicts = {m["name"]: m["verdict"] for m in body["metrics"]}
    assert verdicts["answer_score_mean"] == "same"


async def test_compare_returns_reasons_for_runs_it_cant_compare(
    results: Path,
) -> None:
    resp = await _get("/evals/compare", baseline="eval-old", candidate="eval-on")

    assert resp.status_code == 200
    body = resp.json()
    assert body["comparable"] is False
    assert any("rubric_version" in reason for reason in body["reasons"])


async def test_compare_with_an_unknown_run_is_a_404(results: Path) -> None:
    resp = await _get("/evals/compare", baseline="eval-off", candidate="eval-nope")

    assert resp.status_code == 404


async def test_the_summary_counts_claim_verdicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#139: verdict counts per run, or null when the run didn't verify."""
    monkeypatch.setattr(api_evals, "RESULTS_DIR", tmp_path)
    case = _run("x").case_scores[0]
    verified = case.model_copy(
        update={
            "verifications": [
                ClaimVerification(
                    claim_id="claim-1",
                    text="t",
                    verdict="STALE",
                    evidence_refs=[],
                    claimed_value=None,
                    recomputed_value=None,
                    tolerance=None,
                    reasoning="r",
                )
            ]
        }
    )
    for run in (
        _run("eval-verified", case_scores=[verified], verifier_version="1:tol=0"),
        _run("eval-plain", timestamp_utc=T0 - timedelta(hours=1)),
    ):
        (tmp_path / f"{run.run_id}.json").write_text(run.model_dump_json())

    runs = (await _get("/evals")).json()["runs"]

    assert runs[0]["claim_verdicts"]["STALE"] == 1
    assert runs[0]["claim_verdicts"]["SUPPORTED"] == 0
    assert runs[1]["claim_verdicts"] is None
