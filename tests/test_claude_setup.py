"""The Claude Code project setup in `.claude/`: hooks, settings, rules, agents.

Hooks run as subprocesses fed the same JSON Claude Code sends on stdin, so
these tests exercise the real scripts with no model and no network. See
docs/adr/0017-claude-code-project-setup.md.
"""

import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / ".claude" / "hooks"

pytestmark = pytest.mark.unit


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway git repo on a feature branch, with one commit."""
    _git(tmp_path, "init", "-q", "-b", "feat/x")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "test")
    (tmp_path / "README.md").write_text("x\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("RECON_")}
    env.pop("GITHUB_ACTIONS", None)
    return {**env, **extra}


def run_hook(
    name: str, event: Mapping[str, object], env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HOOKS / name)],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        env=env if env is not None else _clean_env(),
        check=False,
    )


def guard(command: str, cwd: Path, env: dict[str, str] | None = None) -> int:
    event = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}
    return run_hook("guard_bash.py", event, env).returncode


# --- guard_bash.py -----------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "uv run python -m recon.cli eval",
        "uv run python -m recon.cli eval --mode multi --runtime langgraph",
        "make check && uv run python -m recon.cli eval",
        "git push origin main",
        "git push origin HEAD:main",
        "git push origin +main",
        "git push origin refs/heads/main",
        "git -c credential.helper= -c 'credential.helper=!gh auth x' push origin main",
        "git -C . --no-pager push origin main",
        "git push --force origin feat/x",
        "git push -f origin feat/x",
        "git push -uf origin feat/x",
        "git commit --no-verify -m wip",
        "psql -c 'DROP TABLE review_flags'",
        "docker compose exec postgres psql -c 'drop table review_flags'",
        "echo 'truncate table review_flags' | psql",
        "psql -c 'TRUNCATE review_flags'",
        "cat docker/.env",
        "grep PASSWORD .env",
    ],
)
def test_guard_blocks(command: str, repo: Path) -> None:
    assert guard(command, repo) == 2


@pytest.mark.parametrize(
    "command",
    [
        "uv run python -m recon.cli eval --limit 3",
        "uv run python -m recon.cli eval --limit=3 --mode multi",
        "uv run python -m recon.cli run --case-id abc",
        "git push -u origin feat/main-menu",
        "git push origin main-menu",
        "git push --force-with-lease origin feat/x",
        "git -c credential.helper= -c 'credential.helper=!gh auth x' push -u origin feat/x",
        "git push",
        "git push origin HEAD",
        "git commit -m 'explain how to push to main'",
        "cat docker/.env.example",
        "cp docker/.env.example docker/.env",
        "truncate -s 0 build.log",
        "git commit -m 'recon.cli eval: enforce --limit'",
        "git commit -m 'guard: block DROP TABLE and TRUNCATE via psql'",
        "gh pr create --body 'recon.cli eval without --limit is blocked'",
        "make check",
    ],
)
def test_guard_allows(command: str, repo: Path) -> None:
    assert guard(command, repo) == 0


def test_guard_judges_git_dash_c_pushes_by_that_repo(
    repo: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    _git(
        other,
        "-c",
        "user.email=t@example.com",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
    )
    assert guard(f"git -C {other} push", repo) == 2  # other repo is on main
    assert guard("git push", repo) == 0  # this repo is on feat/x


def test_guard_blocks_a_bare_push_while_on_main(repo: Path) -> None:
    _git(repo, "switch", "-q", "-c", "main")
    assert guard("git push", repo) == 2
    assert guard("git push -u origin", repo) == 2
    assert guard("git push origin HEAD", repo) == 2
    assert guard("git push -u origin +HEAD", repo) == 2


def test_guard_is_fast_on_adversarial_quoting(repo: Path) -> None:
    """CodeQL flagged a regex version of the push check for exponential
    backtracking on repeated `"" -c ` input."""
    command = "git" + ' -c ""' * 5000 + " push origin main"
    started = time.perf_counter()
    assert guard(command, repo) == 2
    assert time.perf_counter() - started < 5


def test_guard_explains_the_block(repo: Path) -> None:
    event = {
        "tool_input": {"command": "uv run python -m recon.cli eval"},
        "cwd": str(repo),
    }
    result = run_hook("guard_bash.py", event)
    assert "--limit" in result.stderr
    assert "Do not retry" in result.stderr


def test_guard_override_only_from_the_process_environment(repo: Path) -> None:
    command = "uv run python -m recon.cli eval"
    assert guard(f"RECON_ALLOW_GUARDED=1 {command}", repo) == 2
    assert guard(command, repo, env=_clean_env(RECON_ALLOW_GUARDED="1")) == 0


def test_guard_ignores_malformed_input() -> None:
    result = subprocess.run(
        [sys.executable, str(HOOKS / "guard_bash.py")],
        input="not json",
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0


# --- stop_gate.py ------------------------------------------------------------


def _with_make_test(repo: Path, *, passes: bool) -> Path:
    """Give `repo` a `make test` that passes or fails and records each run."""
    runs = repo / "test-runs.log"
    status = 0 if passes else 1
    (repo / "Makefile").write_text(
        f"test:\n\t@echo run >> {runs}\n\t@echo 'FAILED tests/test_x.py'\n\t@exit {status}\n"
    )
    _git(repo, "add", "Makefile")
    _git(repo, "commit", "-q", "-m", "make")
    return runs


def stop(repo: Path, **fields: object) -> subprocess.CompletedProcess[str]:
    event = {"hook_event_name": "Stop", "cwd": str(repo), **fields}
    return run_hook("stop_gate.py", event)


def _prompt() -> dict[str, str]:
    return {"session_id": uuid.uuid4().hex, "prompt_id": uuid.uuid4().hex}


def test_stop_gate_skips_when_no_python_changed(repo: Path) -> None:
    runs = _with_make_test(repo, passes=False)
    (repo / "notes.md").write_text("changed\n")
    assert stop(repo, **_prompt()).returncode == 0
    assert not runs.exists()


def test_stop_gate_blocks_once_then_warns_the_user(repo: Path) -> None:
    _with_make_test(repo, passes=False)
    (repo / "pkg").mkdir()
    (repo / "pkg" / "new_module.py").write_text("x = 1\n")
    prompt = _prompt()

    first = stop(repo, **prompt)
    assert first.returncode == 2
    assert "FAILED tests/test_x.py" in first.stderr

    # additionalContext would start another turn; a systemMessage doesn't.
    second = stop(repo, **prompt)
    assert second.returncode == 0
    output = json.loads(second.stdout)
    assert "still fails" in output["systemMessage"]
    assert "hookSpecificOutput" not in output

    assert stop(repo, **_prompt()).returncode == 2  # a new prompt checks again


def test_stop_gate_falls_back_to_stop_hook_active(repo: Path) -> None:
    _with_make_test(repo, passes=False)
    (repo / "a.py").write_text("x = 1\n")
    # Claude Code sends false on a prompt's first stop, true after a block.
    assert stop(repo, stop_hook_active=False).returncode == 2
    assert stop(repo, stop_hook_active=True).returncode == 0


def test_stop_gate_passes_when_tests_pass(repo: Path) -> None:
    runs = _with_make_test(repo, passes=True)
    (repo / "a.py").write_text("x = 1\n")
    assert stop(repo, **_prompt()).returncode == 0
    assert runs.read_text().count("run") == 1


def test_stop_gate_sees_renamed_and_ignores_deleted_files(repo: Path) -> None:
    runs = _with_make_test(repo, passes=True)
    (repo / "old.py").write_text("x = 1\n")
    (repo / "gone.py").write_text("y = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "py")

    _git(repo, "rm", "-q", "gone.py")
    assert stop(repo, **_prompt()).returncode == 0
    assert not runs.exists()

    _git(repo, "mv", "old.py", "renamed.py")
    assert stop(repo, **_prompt()).returncode == 0
    assert runs.exists()


@pytest.mark.parametrize("env", [{"GITHUB_ACTIONS": "true"}, {"RECON_STOP_GATE": "0"}])
def test_stop_gate_is_off_in_ci_and_on_request(repo: Path, env: dict[str, str]) -> None:
    runs = _with_make_test(repo, passes=False)
    (repo / "a.py").write_text("x = 1\n")
    event = {"cwd": str(repo), **_prompt()}
    assert run_hook("stop_gate.py", event, _clean_env(**env)).returncode == 0
    assert not runs.exists()


# --- format_python.py --------------------------------------------------------


def test_format_hook_formats_python_and_leaves_other_files(tmp_path: Path) -> None:
    source = tmp_path / "messy.py"
    source.write_text("import os\nx=[1,2 ,3]\n")
    other = tmp_path / "notes.md"
    other.write_text("x=[1,2 ,3]\n")

    for path in (source, other):
        event = {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(path)},
            "cwd": str(ROOT),
        }
        assert run_hook("format_python.py", event).returncode == 0

    assert source.read_text() == "import os\n\nx = [1, 2, 3]\n"  # import kept
    assert other.read_text() == "x=[1,2 ,3]\n"


# --- settings, rules, agents, instruction files -------------------------------

CLAUDE_DIR = ROOT / ".claude"
READ_ONLY_TOOLS = {"Read", "Grep", "Glob", "Bash"}


def _frontmatter(path: Path) -> dict[str, object]:
    text = path.read_text()
    assert text.startswith("---\n"), f"{path.name} has no frontmatter"
    loaded = yaml.safe_load(text.split("---\n")[1])
    assert isinstance(loaded, dict)
    return loaded


def test_settings_hooks_point_at_existing_scripts() -> None:
    settings = json.loads((CLAUDE_DIR / "settings.json").read_text())
    commands = [
        hook["command"]
        for groups in settings["hooks"].values()
        for group in groups
        for hook in group["hooks"]
    ]
    assert len(commands) == 3
    for command in commands:
        script = command.split("$CLAUDE_PROJECT_DIR/")[1].rstrip('"')
        assert (ROOT / script).is_file(), script
        assert os.access(ROOT / script, os.X_OK), f"{script} is not executable"


@pytest.mark.parametrize(
    "rule", sorted((CLAUDE_DIR / "rules").glob("*.md")), ids=lambda p: p.name
)
def test_every_rule_path_matches_a_file(rule: Path) -> None:
    patterns = _frontmatter(rule)["paths"]
    assert isinstance(patterns, list) and patterns
    for pattern in patterns:
        assert any(p.is_file() for p in ROOT.glob(pattern)), (
            f"{rule.name}: {pattern!r} matches no file, so the rule never loads"
        )


@pytest.mark.parametrize(
    "agent", sorted((CLAUDE_DIR / "agents").glob("*.md")), ids=lambda p: p.name
)
def test_subagents_are_read_only_and_described(agent: Path) -> None:
    front = _frontmatter(agent)
    assert front["name"] == agent.stem
    description = front["description"]
    assert isinstance(description, str) and 0 < len(description) < 1536
    tools = {t.strip() for t in str(front["tools"]).split(",")}
    assert tools <= READ_ONLY_TOOLS, (
        f"{agent.name} may write: {tools - READ_ONLY_TOOLS}"
    )


def test_claude_md_imports_agents_md_and_both_stay_short() -> None:
    claude_md = (ROOT / "CLAUDE.md").read_text()
    agents_md = (ROOT / "AGENTS.md").read_text()
    assert claude_md.splitlines()[0] == "@AGENTS.md"
    assert "<!-- rules:start -->" in agents_md and "<!-- rules:end -->" in agents_md
    for text in (claude_md, agents_md):
        assert len(text.splitlines()) < 200
