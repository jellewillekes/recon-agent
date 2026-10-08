"""`scripts/check_prompt_baseline.py`, the pre-commit hook and CI step that
fails when a prompt changes without `evals/baseline.json` being regenerated.

Runs the real script as a subprocess, as pre-commit does, against a baseline
and prompts written to a temp dir. See docs/ci.md.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from recon.eval.hashing import compute_prompt_hashes, prompts_read

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_prompt_baseline.py"
RESULT = ROOT / "evals" / "results" / "eval-20260907T102452Z.json"

pytestmark = pytest.mark.unit


def _check(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--baseline",
            str(tmp_path / "baseline.json"),
            "--prompts-dir",
            str(tmp_path / "prompts"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def scored(tmp_path: Path) -> Path:
    """Two prompts and a baseline whose hashes match them."""
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "investigator.md").write_text("Answer from the tools.\n")
    (prompts / "critic.md").write_text("Check the answer.\n")
    baseline = json.loads(RESULT.read_text(encoding="utf-8"))
    baseline["prompt_hashes"] = compute_prompt_hashes(prompts)
    (tmp_path / "baseline.json").write_text(json.dumps(baseline))
    return tmp_path


def test_matching_prompts_pass(scored: Path) -> None:
    result = _check(scored)
    assert result.returncode == 0, result.stdout


def test_a_changed_prompt_without_a_new_baseline_fails(scored: Path) -> None:
    (scored / "prompts" / "investigator.md").write_text("Guess if unsure.\n")
    result = _check(scored)
    assert result.returncode == 1
    assert "prompts/investigator.md: changed since the baseline" in result.stdout
    assert "critic" not in result.stdout


def test_removing_a_prompt_the_baseline_reads_fails(scored: Path) -> None:
    (scored / "prompts" / "investigator.md").unlink()
    result = _check(scored)
    assert result.returncode == 1
    assert "prompts/investigator.md: removed since the baseline" in result.stdout


def test_a_pinned_prompt_missing_from_the_baseline_fails(scored: Path) -> None:
    """A prompt the baseline's run reads but didn't record, e.g. a role added
    after it was scored (review of #126)."""
    baseline = json.loads((scored / "baseline.json").read_text())
    del baseline["prompt_hashes"]["investigator"]
    baseline["prompt_hashes"]["critic"] = compute_prompt_hashes(scored / "prompts")[
        "critic"
    ]
    (scored / "baseline.json").write_text(json.dumps(baseline))
    result = _check(scored)
    assert result.returncode == 1
    assert "prompts/investigator.md: added since the baseline" in result.stdout


def test_a_prompt_the_baselines_runtime_never_reads_may_change(scored: Path) -> None:
    """ADR 0032: the baseline is a single-mode agent_sdk run, which reads only
    the investigator prompt (and the judges'). Other prompts aren't pinned."""
    (scored / "prompts" / "critic.md").write_text("Reject everything.\n")
    (scored / "prompts" / "worker.md").write_text("Look it up.\n")
    result = _check(scored)
    assert result.returncode == 0, result.stdout
    assert "Not read by the baseline's agent_sdk/single run" in result.stdout
    assert "prompts/critic.md" in result.stdout


def test_a_multi_mode_baseline_pins_its_roles_prompts(scored: Path) -> None:
    baseline = json.loads((scored / "baseline.json").read_text())
    baseline["mode"] = "multi"
    (scored / "baseline.json").write_text(json.dumps(baseline))
    (scored / "prompts" / "critic.md").write_text("Reject everything.\n")
    result = _check(scored)
    assert result.returncode == 1
    assert "prompts/critic.md: changed since the baseline" in result.stdout


def test_each_runtime_and_mode_reads_prompts_that_exist() -> None:
    """The mapping names real files, so a renamed prompt can't drop out of
    the check unnoticed."""
    roles = ["supervisor", "worker_lookup", "worker_facts", "critic"]
    existing = {path.stem for path in (ROOT / "prompts").glob("*.md")}
    for runtime in ("agent_sdk", "langgraph"):
        for mode in ("single", "multi"):
            read = prompts_read(runtime, mode, roles)
            assert read is not None and read <= existing, (runtime, mode)
    langgraph_multi = prompts_read("langgraph", "multi", roles) or frozenset()
    assert "supervisor_langgraph" in langgraph_multi
    assert "supervisor" not in langgraph_multi
    assert prompts_read("other", "single", roles) is None


def test_no_baseline_yet_passes_with_a_notice(tmp_path: Path) -> None:
    (tmp_path / "prompts").mkdir()
    result = _check(tmp_path)
    assert result.returncode == 0
    assert "doesn't exist yet" in result.stdout
