"""Tests for src/recon/cli.py.

compute_dataset_stats is a pure function (no I/O), so it's tested directly
rather than through the CLI/argparse wiring or the network-fetching path.
`_cmd_eval` is exercised through `build_parser` with `fetch_csv`/`load_cases`/
`run_evaluation` monkeypatched, so it never touches the network or a model.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from recon import cli
from recon.cli import build_parser, compute_dataset_stats
from recon.contracts import Case, EvalRun


def _case(
    case_id: str, tags: list[str], expected_tool_path: list[str] | None = None
) -> Case:
    return Case(
        case_id=case_id,
        source="finance-agent-bench",
        question="q",
        expected_answer="a",
        expected_tool_path=expected_tool_path,
        context={},
        tags=tags,
        license="MIT",
        attribution="attribution",
    )


@pytest.mark.unit
def test_compute_dataset_stats_counts_cases_and_tags() -> None:
    cases = [
        _case("1", ["Trends"]),
        _case("2", ["Trends"]),
        _case("3", ["Market Analysis"]),
    ]

    stats = compute_dataset_stats(cases)

    assert stats["case_count"] == 3
    assert stats["tag_counts"] == {"Trends": 2, "Market Analysis": 1}


@pytest.mark.unit
def test_compute_dataset_stats_counts_expected_tool_path() -> None:
    cases = [
        _case("1", ["Trends"], expected_tool_path=["search"]),
        _case("2", ["Trends"], expected_tool_path=None),
    ]

    stats = compute_dataset_stats(cases)

    assert stats["cases_with_expected_tool_path"] == 1


@pytest.mark.unit
def test_compute_dataset_stats_on_empty_dataset() -> None:
    stats = compute_dataset_stats([])

    assert stats["case_count"] == 0
    assert stats["tag_counts"] == {}
    assert stats["cases_with_expected_tool_path"] == 0


def _eval_run(**overrides: object) -> EvalRun:
    defaults: dict[str, object] = {
        "run_id": "eval-fixed",
        "timestamp_utc": datetime.now(UTC),
        "dataset": "finance-agent-bench",
        "dataset_license": "MIT",
        "dataset_attribution": "attribution",
        "runtime": "agent_sdk",
        "mode": "single",
        "model_config_hash": "abc",
        "prompt_hashes": {"investigator": "abc"},
        "rubric_version": "1",
        "case_scores": [],
        "aggregate": {"task_completion_rate": 1.0, "answer_score_mean": 1.0},
        "total_cost_eur": 0.05,
    }
    defaults.update(overrides)
    return EvalRun(**defaults)  # type: ignore[arg-type]


def _patch_dataset_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "fetch_csv", lambda path: path)
    monkeypatch.setattr(cli, "load_cases", lambda path: [_case("1", ["Trends"])])


@pytest.mark.unit
def test_cmd_eval_writes_json_and_markdown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_dataset_loading(monkeypatch)
    monkeypatch.setattr(
        cli, "run_evaluation", lambda cases, runtime, limit: _eval_run()
    )
    monkeypatch.chdir(tmp_path)

    args = build_parser().parse_args(["eval", "--limit", "2"])
    args.func(args)

    assert (tmp_path / "evals" / "results" / "eval-fixed.json").exists()
    assert (tmp_path / "evals" / "results" / "eval-fixed.md").exists()


@pytest.mark.unit
def test_cmd_eval_mode_flag_reaches_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_dataset_loading(monkeypatch)
    captured: dict[str, object] = {}

    def fake_run_evaluation(cases: object, runtime: object, limit: object) -> EvalRun:
        captured["mode"] = runtime._mode  # type: ignore[attr-defined]
        return _eval_run()

    monkeypatch.setattr(cli, "run_evaluation", fake_run_evaluation)
    monkeypatch.chdir(tmp_path)

    args = build_parser().parse_args(["eval", "--mode", "multi"])
    args.func(args)

    assert captured["mode"] == "multi"


@pytest.mark.unit
def test_cmd_eval_defaults_to_single_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_dataset_loading(monkeypatch)
    captured: dict[str, object] = {}

    def fake_run_evaluation(cases: object, runtime: object, limit: object) -> EvalRun:
        captured["mode"] = runtime._mode  # type: ignore[attr-defined]
        return _eval_run()

    monkeypatch.setattr(cli, "run_evaluation", fake_run_evaluation)
    monkeypatch.chdir(tmp_path)

    args = build_parser().parse_args(["eval"])
    args.func(args)

    assert captured["mode"] == "single"


@pytest.mark.unit
def test_cmd_eval_gate_passes_prints_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_dataset_loading(monkeypatch)
    monkeypatch.setattr(
        cli, "run_evaluation", lambda cases, runtime, limit: _eval_run()
    )
    monkeypatch.chdir(tmp_path)
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(_eval_run().model_dump_json(), encoding="utf-8")

    args = build_parser().parse_args(["eval", "--baseline", str(baseline_path)])
    args.func(args)  # must not raise


@pytest.mark.unit
def test_cmd_eval_gate_failure_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_dataset_loading(monkeypatch)
    monkeypatch.setattr(
        cli,
        "run_evaluation",
        lambda cases, runtime, limit: _eval_run(
            aggregate={"task_completion_rate": 0.5, "answer_score_mean": 0.5}
        ),
    )
    monkeypatch.chdir(tmp_path)
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(
        _eval_run(
            aggregate={"task_completion_rate": 0.9, "answer_score_mean": 0.9}
        ).model_dump_json(),
        encoding="utf-8",
    )

    args = build_parser().parse_args(["eval", "--baseline", str(baseline_path)])
    with pytest.raises(SystemExit) as exc_info:
        args.func(args)

    assert exc_info.value.code == 1
