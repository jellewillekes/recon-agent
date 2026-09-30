"""`scripts/check_hygiene.py`, the company-name check (#76).

Runs the real script in throwaway git repos with invented names, so no real
name appears here. The check must report where a name is, never the name.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_hygiene.py"
NAME = "Zorblax Widgets"

pytestmark = pytest.mark.unit


def _git(cwd: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)


def _run(repo: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    scanned = ("PR_TITLE", "PR_BODY", "HYGIENE_DENYLIST")
    clean = {k: v for k, v in os.environ.items() if k not in scanned}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=repo,
        env={**clean, **env},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "clean.md").write_text("A widget maker, described by role.\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "docs: describe a market participant")
    return tmp_path


def _commit(repo: Path, path: str, text: str, message: str = "change") -> None:
    (repo / path).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def test_a_clean_repo_passes(repo: Path) -> None:
    result = _run(repo, "check", "--commits", "HEAD", HYGIENE_DENYLIST=NAME)
    assert result.returncode == 0, result.stdout
    assert "1 names checked" in result.stdout


def test_a_name_in_a_file_fails_with_the_location_only(repo: Path) -> None:
    _commit(repo, "notes.md", "intro\nsee zorblax-widgets' filing\n")
    result = _run(repo, "check", HYGIENE_DENYLIST=NAME)
    assert result.returncode == 1
    assert "notes.md:2" in result.stdout
    assert "zorblax" not in (result.stdout + result.stderr).lower()


def test_a_name_in_a_commit_message_fails(repo: Path) -> None:
    _commit(repo, "a.md", "x\n", "fix: handle Zorblax Widgets\n\nbody")
    result = _run(repo, "check", "--commits", "HEAD~1..HEAD", HYGIENE_DENYLIST=NAME)
    assert result.returncode == 1
    assert "message line 1" in result.stdout
    assert "zorblax" not in result.stdout.lower()


def test_a_commit_message_being_written_fails(repo: Path) -> None:
    message = repo / "COMMIT_EDITMSG"
    message.write_text("docs: tidy\n\nAsked by zorblax widgets.\n")
    result = _run(repo, "check", "--message-file", str(message), HYGIENE_DENYLIST=NAME)
    assert result.returncode == 1
    assert result.stdout.splitlines()[0] == "commit message line 3"


def test_a_name_in_the_pr_title_or_body_fails(repo: Path) -> None:
    result = _run(
        repo,
        "check",
        HYGIENE_DENYLIST=NAME,
        PR_TITLE="feat: ZORBLAX WIDGETS support",
        PR_BODY="Summary\n\nFor Zorblax  Widgets.\n",
    )
    assert result.returncode == 1
    assert result.stdout.splitlines()[:2] == ["PR title", "PR body line 3"]


def test_names_match_whole_words_only(repo: Path) -> None:
    _commit(repo, "a.md", "Zorblax Widgetsmith and preZorblax Widgets\n")
    result = _run(repo, "check", HYGIENE_DENYLIST=NAME)
    assert result.returncode == 0, result.stdout


def test_comments_and_blank_lines_in_the_list_are_ignored(repo: Path) -> None:
    _commit(repo, "a.md", "a widget maker\n")
    (repo / ".hygiene-denylist").write_text("# widget\n\nZorblax  # a comment\n")
    result = _run(repo, "check")
    assert result.returncode == 0, result.stdout
    assert "1 names checked" in result.stdout


def test_a_missing_list_is_a_notice_unless_required(repo: Path) -> None:
    assert _run(repo, "check").returncode == 0
    required = _run(repo, "check", "--require-list")
    assert required.returncode == 2
    assert "HYGIENE_DENYLIST" in required.stdout


def test_propose_drafts_short_names_from_a_tickers_file(tmp_path: Path) -> None:
    tickers = tmp_path / "tickers.txt"
    tickers.write_text(
        "# header\n"
        "ZBX\t1\tname\tZORBLAX WIDGETS & CO.\tcase-1\n"
        "QUX\t2\tticker\tThe Quxley Corp\tcase-2\n"
        "RAW\t3\n"
    )
    out = tmp_path / "list"
    result = _run(tmp_path, "propose", str(tickers), "--out", str(out))
    assert result.returncode == 0, result.stdout
    assert out.read_text().splitlines()[-2:] == ["Quxley", "Zorblax Widgets"]
    assert _run(tmp_path, "propose", str(tickers), "--out", str(out)).returncode == 1
