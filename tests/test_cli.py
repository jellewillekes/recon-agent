"""Tests for src/recon/cli.py.

compute_dataset_stats is a pure function (no I/O), so it's tested directly
rather than through the CLI/argparse wiring or the network-fetching path.
`_cmd_eval` and the `edgar` subcommands are exercised through `build_parser`
with the adapters' functions monkeypatched, so neither touches the network,
a model, or the real `data/` directories.
"""

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Self

import pytest

from recon import cli, cli_edgar
from recon.adapters.finance_agent_bench import DATASET_ID
from recon.cli import build_parser, compute_dataset_stats
from recon.contracts import Case, CaseScore, EvalRun
from recon.eval.gate import SKIPPED_AT_COST_CAP
from recon.eval.harness import RUBRIC_VERSION
from recon.eval.thresholds import load_thresholds
from recon.tools import data_source


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


def _score(case_id: str) -> CaseScore:
    return CaseScore(
        case_id=case_id,
        task_completion=True,
        answer_score=1.0,
        tool_path_exact=False,
        tool_path_equivalent=False,
        tool_call_accuracy=1.0,
        rubric_scores={},
        cost_eur=0.05,
        elapsed_ms=1,
        notes="",
    )


def _eval_run(**overrides: object) -> EvalRun:
    defaults: dict[str, object] = {
        "run_id": "eval-fixed",
        "timestamp_utc": datetime.now(UTC),
        "dataset": DATASET_ID,
        "dataset_license": "MIT",
        "dataset_attribution": "attribution",
        "runtime": "agent_sdk",
        "mode": "single",
        "model_config_hash": "abc",
        "prompt_hashes": {"investigator": "abc"},
        "rubric_version": RUBRIC_VERSION,
        "case_scores": [_score("1")],
        "aggregate": {"task_completion_rate": 1.0, "answer_score_mean": 1.0},
        "total_cost_eur": 0.05,
        "tool_data_snapshot": data_source.tool_data_snapshot_id(),
    }
    defaults.update(overrides)
    return EvalRun(**defaults)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _repo_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most tests here chdir to tmp_path; read the repo's thresholds anyway."""
    path = Path(__file__).resolve().parent.parent / "config" / "thresholds.yaml"
    monkeypatch.setattr(cli, "load_thresholds", lambda: load_thresholds(path))


def _patch_dataset_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "fetch_csv", lambda path: path)
    monkeypatch.setattr(cli, "load_cases", lambda path: [_case("1", ["Trends"])])


@pytest.mark.unit
def test_cmd_eval_writes_json_and_markdown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_dataset_loading(monkeypatch)
    monkeypatch.setattr(
        cli,
        "run_evaluation",
        lambda cases, runtime, tool_data_snapshot, max_cost_eur: _eval_run(),
    )
    monkeypatch.chdir(tmp_path)

    args = build_parser().parse_args(["eval", "--limit", "2"])
    args.func(args)

    json_text = (tmp_path / "evals" / "results" / "eval-fixed.json").read_text()
    assert json_text.endswith("}\n")
    assert (tmp_path / "evals" / "results" / "eval-fixed.md").exists()


@pytest.mark.unit
def test_cmd_eval_mode_flag_reaches_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_dataset_loading(monkeypatch)
    captured: dict[str, object] = {}

    def fake_run_evaluation(
        cases: object,
        runtime: object,
        tool_data_snapshot: object,
        max_cost_eur: object,
    ) -> EvalRun:
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

    def fake_run_evaluation(
        cases: object,
        runtime: object,
        tool_data_snapshot: object,
        max_cost_eur: object,
    ) -> EvalRun:
        captured["mode"] = runtime._mode  # type: ignore[attr-defined]
        captured["max_cost_eur"] = max_cost_eur
        return _eval_run()

    monkeypatch.setattr(cli, "run_evaluation", fake_run_evaluation)
    monkeypatch.chdir(tmp_path)

    args = build_parser().parse_args(["eval"])
    args.func(args)

    assert captured["mode"] == "single"
    assert captured["max_cost_eur"] == cli.DEFAULT_MAX_COST_EUR


@pytest.mark.unit
def test_cmd_eval_runtime_flag_reaches_langgraph_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_dataset_loading(monkeypatch)
    captured: dict[str, object] = {}

    def fake_run_evaluation(
        cases: object,
        runtime: object,
        tool_data_snapshot: object,
        max_cost_eur: object,
    ) -> EvalRun:
        captured["runtime_type"] = type(runtime).__name__
        return _eval_run()

    monkeypatch.setattr(cli, "run_evaluation", fake_run_evaluation)
    monkeypatch.chdir(tmp_path)

    args = build_parser().parse_args(["eval", "--runtime", "langgraph"])
    args.func(args)

    assert captured["runtime_type"] == "LangGraphRuntime"


@pytest.mark.unit
def test_cmd_eval_defaults_to_sdk_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_dataset_loading(monkeypatch)
    captured: dict[str, object] = {}

    def fake_run_evaluation(
        cases: object,
        runtime: object,
        tool_data_snapshot: object,
        max_cost_eur: object,
    ) -> EvalRun:
        captured["runtime_type"] = type(runtime).__name__
        return _eval_run()

    monkeypatch.setattr(cli, "run_evaluation", fake_run_evaluation)
    monkeypatch.chdir(tmp_path)

    args = build_parser().parse_args(["eval"])
    args.func(args)

    assert captured["runtime_type"] == "AgentSdkRuntime"


@pytest.mark.unit
def test_build_runtime_langgraph_multi_builds_a_multi_mode_runtime() -> None:
    """Issue #14 part 2: `--runtime langgraph --mode multi` used to raise
    `NotImplementedError` (caught by `_build_runtime` into a clean
    `SystemExit`, round 2 review of PR #46) - now a real, constructible
    runtime, the same as every other runtime/mode combination.
    """
    from recon.runtimes.langgraph import LangGraphRuntime

    runtime = cli._build_runtime("langgraph", "multi")

    assert isinstance(runtime, LangGraphRuntime)


@pytest.mark.unit
def test_cmd_eval_gate_passes_prints_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_dataset_loading(monkeypatch)
    monkeypatch.setattr(
        cli,
        "run_evaluation",
        lambda cases, runtime, tool_data_snapshot, max_cost_eur: _eval_run(),
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
        lambda cases, runtime, tool_data_snapshot, max_cost_eur: _eval_run(
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


@pytest.mark.unit
def test_cmd_eval_records_the_tool_data_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_dataset_loading(monkeypatch)
    seen: dict[str, object] = {}

    def fake_run_evaluation(
        cases: object,
        runtime: object,
        tool_data_snapshot: object,
        max_cost_eur: object,
    ) -> EvalRun:
        seen["snapshot"] = tool_data_snapshot
        return _eval_run()

    monkeypatch.setattr(cli, "run_evaluation", fake_run_evaluation)
    monkeypatch.chdir(tmp_path)
    args = build_parser().parse_args(["eval", "--limit", "1"])
    args.func(args)

    # tests/conftest.py pins the fixture.
    assert seen["snapshot"] == data_source.tool_data_snapshot_id()
    assert str(seen["snapshot"]).startswith("fixture-")


@pytest.mark.unit
@pytest.mark.parametrize(
    "baseline_overrides",
    [
        {"rubric_version": "1"},
        {"dataset": "other-dataset"},
        {"tool_data_snapshot": "20260928"},
        {"tool_data_snapshot": None},
        {"case_scores": [_score("1"), _score("2")]},
        {"aggregate": {"task_completion_rate": 1.0, SKIPPED_AT_COST_CAP: 3.0}},
    ],
    ids=["rubric", "dataset", "snapshot", "no-snapshot", "cases", "capped"],
)
def test_cmd_eval_refuses_an_incomparable_baseline_before_running(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    baseline_overrides: dict[str, object],
) -> None:
    _patch_dataset_loading(monkeypatch)

    def must_not_run(*args: object, **kwargs: object) -> EvalRun:
        raise AssertionError("the eval ran, spending credit, before refusing")

    monkeypatch.setattr(cli, "run_evaluation", must_not_run)
    monkeypatch.chdir(tmp_path)
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(
        _eval_run(**baseline_overrides).model_dump_json(), encoding="utf-8"
    )

    args = build_parser().parse_args(["eval", "--baseline", str(baseline_path)])
    with pytest.raises(SystemExit) as exc_info:
        args.func(args)

    assert exc_info.value.code == 1


@pytest.mark.unit
def test_cmd_eval_fails_on_a_missing_edgar_cache_before_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_dataset_loading(monkeypatch)
    monkeypatch.setenv("RECON_TOOL_DATA", "edgar")
    monkeypatch.chdir(tmp_path)  # no data/processed/sec_edgar here

    def must_not_run(*args: object, **kwargs: object) -> EvalRun:
        raise AssertionError("the eval started without tool data")

    monkeypatch.setattr(cli, "run_evaluation", must_not_run)
    args = build_parser().parse_args(["eval", "--limit", "1"])
    with pytest.raises(SystemExit, match="edgar fetch"):
        args.func(args)


class _FakeHttpClient:
    """Stands in for the `httpx.Client` `with` block in `_cmd_edgar_fetch`."""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _edgar_config() -> object:
    return cli.sec_edgar.EdgarConfig(
        filed_cutoff=date(2025, 4, 7),
        taxonomies=("us-gaap",),
        forms=("10-K",),
        max_requests_per_second=5.0,
    )


def _patch_edgar_client_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.sec_edgar, "load_config", lambda path: _edgar_config())
    monkeypatch.setattr(
        cli.sec_edgar, "user_agent_from_env", lambda: "Test test@example.com"
    )
    monkeypatch.setattr(
        cli.sec_edgar, "build_client", lambda user_agent: _FakeHttpClient()
    )
    monkeypatch.setattr(cli.sec_edgar, "EdgarClient", lambda *a, **k: "edgar-client")


@pytest.mark.unit
def test_edgar_fetch_parser_defaults() -> None:
    args = build_parser().parse_args(["edgar", "fetch"])
    assert args.from_dataset is False
    assert args.path == cli.DEFAULT_DATASET_PATH
    assert args.config == cli.sec_edgar.DEFAULT_CONFIG_PATH
    assert args.func is cli_edgar._cmd_edgar_fetch


@pytest.mark.unit
def test_edgar_stats_parser_wires_the_stats_command() -> None:
    args = build_parser().parse_args(["edgar", "stats"])
    assert args.func is cli_edgar._cmd_edgar_stats


@pytest.mark.unit
def test_cmd_edgar_fetch_from_dataset_stops_before_fetching_companies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--from-dataset` must derive and write the tickers file, then return -
    it must never reach the per-company fetch loop below it."""
    _patch_edgar_client_setup(monkeypatch)
    captured: dict[str, object] = {}

    def fake_derive(edgar: object, dataset_path: Path, tickers_path: Path) -> None:
        captured["dataset_path"] = dataset_path
        captured["tickers_path"] = tickers_path

    monkeypatch.setattr(cli_edgar, "_derive_tickers_file", fake_derive)
    monkeypatch.setattr(
        cli.sec_edgar_tickers,
        "read_tickers_file",
        lambda path: pytest.fail("--from-dataset must not read the tickers file"),
    )

    dataset_path = tmp_path / "dataset.csv"
    args = build_parser().parse_args(
        ["edgar", "fetch", "--from-dataset", "--path", str(dataset_path)]
    )
    args.func(args)

    assert captured["dataset_path"] == dataset_path
    assert captured["tickers_path"] == cli.sec_edgar.DEFAULT_RAW_DIR / "tickers.txt"


@pytest.mark.unit
def test_cmd_edgar_fetch_runs_full_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_edgar_client_setup(monkeypatch)
    monkeypatch.setattr(cli.sec_edgar, "snapshot_id", lambda: "20260928")
    monkeypatch.setattr(
        cli.sec_edgar_tickers, "read_tickers_file", lambda path: {"ABC": 123}
    )

    fetch_calls: list[tuple[object, int, Path]] = []

    def fake_fetch_company(
        edgar: object, cik: int, snapshot_dir: Path
    ) -> dict[str, object]:
        fetch_calls.append((edgar, cik, snapshot_dir))
        return {"submissions": ["x"], "companyfacts": True}

    monkeypatch.setattr(cli.sec_edgar, "fetch_company", fake_fetch_company)

    manifest_calls: dict[str, object] = {}

    def fake_write_manifest(
        snapshot_dir: Path, snapshot: str, config: object, companies: dict[str, object]
    ) -> Path:
        manifest_calls["companies"] = companies
        return snapshot_dir / "manifest.json"

    monkeypatch.setattr(cli.sec_edgar, "write_manifest", fake_write_manifest)

    normalize_calls: dict[str, object] = {}

    def fake_normalize_snapshot(
        snapshot_dir: Path, tickers: dict[str, int], config: object, out_dir: Path
    ) -> dict[str, int]:
        normalize_calls["tickers"] = tickers
        normalize_calls["out_dir"] = out_dir
        return {"companies": 1, "financial_facts": 5}

    monkeypatch.setattr(
        cli_edgar.sec_edgar_normalize, "normalize_snapshot", fake_normalize_snapshot
    )

    args = build_parser().parse_args(["edgar", "fetch"])
    args.func(args)

    assert fetch_calls == [
        ("edgar-client", 123, cli.sec_edgar.DEFAULT_RAW_DIR / "20260928")
    ]
    assert manifest_calls["companies"] == {
        "ABC": {"cik": 123, "submissions": ["x"], "companyfacts": True}
    }
    assert normalize_calls["tickers"] == {"ABC": 123}
    assert (
        normalize_calls["out_dir"] == cli.sec_edgar.DEFAULT_PROCESSED_DIR / "20260928"
    )


@pytest.mark.unit
def test_cmd_edgar_stats_prints_snapshot_summary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot_dir = tmp_path / "20260928"
    snapshot_dir.mkdir()
    (snapshot_dir / cli.sec_edgar.MANIFEST_FILENAME).write_text(
        json.dumps({"filed_cutoff": "2025-04-07", "row_counts": {"companies": 1}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli_edgar.sec_edgar_store, "latest_snapshot", lambda base: snapshot_dir
    )
    monkeypatch.setattr(
        cli_edgar.sec_edgar_store,
        "company_stats",
        lambda directory: [("ABC", 10, 2), ("XYZ", 0, 1)],
    )
    monkeypatch.setattr(
        cli.sec_edgar, "DEFAULT_RAW_DIR", tmp_path / "raw-with-no-tickers-file"
    )

    args = build_parser().parse_args(["edgar", "stats"])
    args.func(args)

    out = capsys.readouterr().out
    assert "snapshot: 20260928" in out
    assert "cutoff: 2025-04-07" in out
    assert "XYZ" in out and "no XBRL facts" in out


@pytest.mark.unit
def test_cmd_edgar_fetch_merges_repeated_tickers_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_edgar_client_setup(monkeypatch)
    monkeypatch.setattr(cli.sec_edgar, "snapshot_id", lambda: "20260928")
    monkeypatch.setattr(
        cli.sec_edgar, "fetch_company", lambda edgar, cik, snapshot_dir: {}
    )
    monkeypatch.setattr(cli.sec_edgar, "write_manifest", lambda *a: tmp_path)
    fetched: dict[str, object] = {}
    monkeypatch.setattr(
        cli_edgar.sec_edgar_normalize,
        "normalize_snapshot",
        lambda snapshot_dir, tickers, config, out_dir: fetched.update(tickers) or {},
    )
    first, second = tmp_path / "a.txt", tmp_path / "b.txt"
    first.write_text("ABC\t1\n", encoding="utf-8")
    second.write_text("XYZ\t2\n", encoding="utf-8")

    args = build_parser().parse_args(
        ["edgar", "fetch", "--tickers-file", str(first), "--tickers-file", str(second)]
    )
    args.func(args)

    assert fetched == {"ABC": 1, "XYZ": 2}


@pytest.mark.unit
def test_cmd_edgar_fetch_from_dataset_needs_exactly_one_tickers_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_edgar_client_setup(monkeypatch)
    args = build_parser().parse_args(
        [
            "edgar",
            "fetch",
            "--from-dataset",
            "--tickers-file",
            "a",
            "--tickers-file",
            "b",
        ]
    )
    with pytest.raises(SystemExit, match="exactly one"):
        args.func(args)


def _capture_cases(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Three cases in the dataset; records which ones reach the harness."""
    monkeypatch.setattr(cli, "fetch_csv", lambda path: path)
    monkeypatch.setattr(
        cli, "load_cases", lambda path: [_case(i, ["Trends"]) for i in "123"]
    )
    seen: dict[str, list[str]] = {}

    def fake_run_evaluation(
        cases: list[Case],
        runtime: object,
        tool_data_snapshot: object,
        max_cost_eur: object,
    ) -> EvalRun:
        seen["ids"] = [case.case_id for case in cases]
        return _eval_run()

    monkeypatch.setattr(cli, "run_evaluation", fake_run_evaluation)
    return seen


@pytest.mark.unit
def test_cmd_eval_runs_only_the_cases_in_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _capture_cases(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cases.txt").write_text("# smoke\n3\n1\n")
    args = build_parser().parse_args(["eval", "--cases", "cases.txt"])
    args.func(args)
    assert seen["ids"] == ["1", "3"]


@pytest.mark.unit
def test_cmd_eval_runs_only_one_companys_cases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _capture_cases(monkeypatch)
    monkeypatch.chdir(tmp_path)
    tickers = tmp_path / "tickers-dataset.txt"
    tickers.write_text("AAA\t1\tname\tAlpha Corp\t2,3\nBBB\t2\tname\tBeta\t1\n")
    args = build_parser().parse_args(
        ["eval", "--company", "aaa", "--tickers-file", str(tickers)]
    )
    args.func(args)
    assert seen["ids"] == ["2", "3"]


@pytest.mark.unit
def test_cmd_eval_refuses_an_unknown_company_before_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _capture_cases(monkeypatch)
    tickers = tmp_path / "tickers.txt"
    tickers.write_text("AAA\t1\tmanual\tAlpha Corp\n")
    args = build_parser().parse_args(
        ["eval", "--company", "AAA", "--tickers-file", str(tickers)]
    )
    with pytest.raises(SystemExit, match="No case ids recorded for AAA"):
        args.func(args)
    assert "ids" not in seen


@pytest.mark.unit
def test_cmd_eval_refuses_an_estimate_over_the_cap_before_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _capture_cases(monkeypatch)
    monkeypatch.chdir(tmp_path)
    cap = 2.5 * cli.ESTIMATED_COST_EUR_PER_CASE  # three cases need 3x
    args = build_parser().parse_args(["eval", "--max-cost-eur", str(cap)])
    with pytest.raises(SystemExit, match="estimate exceeds the cap"):
        args.func(args)
    assert "ids" not in seen
    assert not (tmp_path / "evals").exists()


@pytest.mark.unit
def test_cmd_eval_exits_nonzero_when_the_cap_stopped_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_dataset_loading(monkeypatch)
    capped = _eval_run(
        aggregate={"task_completion_rate": 1.0, SKIPPED_AT_COST_CAP: 2.0}
    )
    monkeypatch.setattr(cli, "run_evaluation", lambda *a, **k: capped)
    monkeypatch.chdir(tmp_path)
    args = build_parser().parse_args(["eval"])
    with pytest.raises(SystemExit, match="before 2 case"):
        args.func(args)
    assert (tmp_path / "evals" / "results" / "eval-fixed.json").exists()


@pytest.mark.unit
def test_cmd_cases_prints_ids_spread_across_companies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _capture_cases(monkeypatch)
    tickers = tmp_path / "tickers-dataset.txt"
    tickers.write_text("AAA\t1\tname\tAlpha\t1,2\nBBB\t2\tname\tBeta\t3\n")
    args = build_parser().parse_args(
        ["cases", "--spread", "2", "--tickers-file", str(tickers)]
    )
    args.func(args)
    assert capsys.readouterr().out.split() == ["1", "3"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "content",
    ["gate: [unclosed\n", "gate: {answer_score_max_relative_drop: 0.02}\n"],
    ids=["bad-yaml", "missing-limit"],
)
def test_cmd_eval_refuses_a_broken_thresholds_file_before_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: str
) -> None:
    seen = _capture_cases(monkeypatch)
    broken = tmp_path / "thresholds.yaml"
    broken.write_text(content)
    monkeypatch.setattr(cli, "load_thresholds", lambda: load_thresholds(broken))
    args = build_parser().parse_args(["eval", "--limit", "1"])
    with pytest.raises(SystemExit, match="thresholds.yaml can't be read"):
        args.func(args)
    assert "ids" not in seen
