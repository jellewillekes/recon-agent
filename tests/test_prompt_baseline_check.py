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

from recon.eval.hashing import compute_prompt_hashes

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


def test_added_and_removed_prompts_fail(scored: Path) -> None:
    (scored / "prompts" / "critic.md").unlink()
    (scored / "prompts" / "worker.md").write_text("Look it up.\n")
    result = _check(scored)
    assert result.returncode == 1
    assert "prompts/critic.md: removed since the baseline" in result.stdout
    assert "prompts/worker.md: added since the baseline" in result.stdout


def test_no_baseline_yet_passes_with_a_notice(tmp_path: Path) -> None:
    (tmp_path / "prompts").mkdir()
    result = _check(tmp_path)
    assert result.returncode == 0
    assert "doesn't exist yet" in result.stdout
