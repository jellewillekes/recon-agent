"""The gate's claim-level check (`eval/claim_gate.py`) and its comparability
rule (docs/contracts.md §9, ADR 0035). Runs are built in memory; no run is made."""

from datetime import UTC, datetime
from typing import Any

from recon.contracts import CaseScore, ClaimVerification, EvalRun, Verdict
from recon.eval.gate import check_gate
from recon.eval.thresholds import GateThresholds

LIMITS = GateThresholds(
    answer_score_noise_band=0.10,
    task_completion_max_case_drop=1,
    cost_max_relative_rise=0.20,
)


def _claim(verdict: Verdict, text: str = "a claim") -> ClaimVerification:
    return ClaimVerification(
        claim_id="c",
        text=text,
        verdict=verdict,
        evidence_refs=[],
        claimed_value=None,
        recomputed_value=None,
        tolerance=None,
        reasoning="r",
    )


def _case(
    case_id: str,
    verdicts: list[Verdict] | None,
    *,
    completed: bool = True,
    texts: list[str] | None = None,
) -> CaseScore:
    claims = (
        None
        if verdicts is None
        else [
            _claim(v, texts[i] if texts else f"claim {i}")
            for i, v in enumerate(verdicts)
        ]
    )
    return CaseScore(
        case_id=case_id,
        task_completion=completed,
        answer_score=1.0,
        tool_path_exact=True,
        tool_path_equivalent=True,
        tool_call_accuracy=1.0,
        rubric_scores={},
        cost_eur=0.01,
        elapsed_ms=1,
        notes="",
        verifications=claims,
    )


def _run(cases: list[CaseScore], **overrides: Any) -> EvalRun:
    fields: dict[str, Any] = {
        "run_id": "eval-1",
        "timestamp_utc": datetime.now(UTC),
        "dataset": "finance-agent-bench",
        "dataset_license": "MIT",
        "dataset_attribution": "attribution",
        "runtime": "agent_sdk",
        "mode": "single",
        "model_config_hash": "abc",
        "prompt_hashes": {"investigator": "abc"},
        "rubric_version": "1",
        "case_scores": cases,
        "aggregate": {"task_completion_rate": 1.0, "answer_score_mean": 1.0},
        "total_cost_eur": 0.10,
        "tool_data_snapshot": "fixture",
        "verifier_version": "1:tol=0.0",
    }
    fields.update(overrides)
    return EvalRun(**fields)


GOOD: list[Verdict] = ["SUPPORTED", "SUPPORTED", "SUPPORTED", "SUPPORTED"]


def test_unchanged_claims_pass() -> None:
    baseline = _run([_case("a", GOOD)])
    assert check_gate(_run([_case("a", GOOD)]), baseline, LIMITS) == []


def test_a_higher_unsupported_rate_fails_and_names_the_case() -> None:
    baseline = _run([_case("a", GOOD), _case("b", GOOD)])
    worse: list[Verdict] = ["SUPPORTED", "SUPPORTED", "UNSUPPORTED", "UNSUPPORTED"]
    candidate = _run([_case("a", worse), _case("b", GOOD)])
    failures = check_gate(candidate, baseline, LIMITS)
    assert len(failures) == 1
    assert "unsupported" in failures[0].lower()
    assert "'a'" in failures[0]


def test_more_claims_at_the_same_rate_pass() -> None:
    one_bad: list[Verdict] = ["SUPPORTED", "SUPPORTED", "SUPPORTED", "UNSUPPORTED"]
    baseline = _run([_case("a", one_bad)])
    candidate = _run([_case("a", one_bad * 2)])
    assert check_gate(candidate, baseline, LIMITS) == []


def test_unverifiable_claims_do_not_count_as_checked() -> None:
    baseline = _run([_case("a", ["SUPPORTED", "UNSUPPORTED"])])
    candidate = _run([_case("a", ["SUPPORTED", "UNSUPPORTED", "UNVERIFIABLE"])])
    assert check_gate(candidate, baseline, LIMITS) == []


def test_a_rise_inside_the_noise_band_passes() -> None:
    baseline = _run([_case("a", GOOD)])
    one_bad: list[Verdict] = ["SUPPORTED", "SUPPORTED", "SUPPORTED", "UNSUPPORTED"]
    candidate = _run([_case("a", one_bad)])
    wide = GateThresholds(
        answer_score_noise_band=0.10,
        task_completion_max_case_drop=1,
        cost_max_relative_rise=0.20,
        claim_bad_rate_noise_band=0.30,
    )
    assert check_gate(candidate, baseline, LIMITS) != []
    assert check_gate(candidate, baseline, wide) == []


def test_a_new_contradiction_in_a_passing_case_fails_even_inside_the_band() -> None:
    many_good: list[Verdict] = ["SUPPORTED"] * 20
    baseline = _run([_case("a", many_good)])
    candidate = _run([_case("a", [*many_good[:19], "CONTRADICTED"])])
    wide = GateThresholds(
        answer_score_noise_band=0.10,
        task_completion_max_case_drop=1,
        cost_max_relative_rise=0.20,
        claim_bad_rate_noise_band=0.50,
    )
    failures = check_gate(candidate, baseline, wide)
    assert len(failures) == 1
    assert "contradicted" in failures[0].lower()


def test_the_failure_quotes_the_contradicted_claim() -> None:
    baseline = _run([_case("a", GOOD)])
    candidate = _run(
        [_case("a", ["CONTRADICTED"], texts=["Revenue was $480.0 million in FY2024."])]
    )
    failures = check_gate(candidate, baseline, LIMITS)
    assert any("Revenue was $480.0 million" in f for f in failures)


def test_a_contradiction_in_a_case_that_already_failed_is_not_new() -> None:
    baseline = _run([_case("a", ["CONTRADICTED", "SUPPORTED"], completed=False)])
    candidate = _run([_case("a", ["CONTRADICTED", "SUPPORTED"])])
    assert check_gate(candidate, baseline, LIMITS) == []


def test_baseline_without_verification_is_not_measured_not_failed() -> None:
    baseline = _run([_case("a", None)], verifier_version=None)
    candidate = _run([_case("a", ["CONTRADICTED"])])
    assert check_gate(candidate, baseline, LIMITS) == []


def test_candidate_that_stopped_verifying_is_refused() -> None:
    baseline = _run([_case("a", GOOD)])
    candidate = _run([_case("a", None)], verifier_version=None)
    failures = check_gate(candidate, baseline, LIMITS)
    assert any("verif" in f for f in failures)


def test_different_verifier_versions_are_not_comparable() -> None:
    baseline = _run([_case("a", GOOD)], verifier_version="1:tol=0.0")
    candidate = _run([_case("a", GOOD)], verifier_version="1:tol=0.5")
    failures = check_gate(candidate, baseline, LIMITS)
    assert any("verifier_version" in f for f in failures)


def test_old_runs_load_without_the_new_fields() -> None:
    run = _run([_case("a", None)], verifier_version=None)
    dumped = run.model_dump(mode="json", exclude={"verifier_version"})
    for case in dumped["case_scores"]:
        del case["verifications"]
    loaded = EvalRun.model_validate(dumped)
    assert loaded.verifier_version is None
    assert loaded.case_scores[0].verifications is None
