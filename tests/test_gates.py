import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FAKE = """#!/bin/sh
echo "$@" >> "$FAKE_LOG"
case "$1 $2" in
  "eval --replay") flag="$FAKE_DIR/fail-replay" ;;
  *) flag="$FAKE_DIR/fail-live" ;;
esac
if [ -e "$flag" ]; then echo '{"passed": false, "cases": ["boom-case"]}'; else echo '{"passed": true}'; fi
"""


def git(cwd, *args, env=None):
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True)


@pytest.fixture
def sandbox(tmp_path, git_env, monkeypatch):
    """A throwaway git repo holding copies of the scripts, plus a fake CLI that logs its argv."""
    for var in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"):
        monkeypatch.delenv(var, raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "scripts").mkdir()
    for name in ("gates.sh", "gate-paths.txt", "install-git-hooks.sh"):
        shutil.copy2(REPO / "scripts" / name, repo / "scripts" / name)
    (repo / ".githooks").mkdir()
    shutil.copy2(REPO / ".githooks/pre-commit", repo / ".githooks/pre-commit")
    fake_dir = tmp_path / "fake"
    fake_dir.mkdir()
    fake = fake_dir / "fake-cli"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    log = fake_dir / "log"
    monkeypatch.setenv("CODE_CHECK_BIN", str(fake))
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_DIR", str(fake_dir))

    class Box:
        pass

    box = Box()
    box.repo, box.fake_dir, box.log = repo, fake_dir, log
    box.calls = lambda: log.read_text().splitlines() if log.exists() else []

    def stage(*paths):
        for p in paths:
            f = repo / p
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x\n")
        git(repo, "add", *paths)

    box.stage = stage
    return box


def run_gates(box, *args, cwd=None, env=None):
    return subprocess.run(
        [str(box.repo / "scripts/gates.sh"), *args],
        cwd=cwd or box.repo, env=env, capture_output=True, text=True, check=False,
    )


def test_gates_runs_replay_always(sandbox):
    sandbox.stage("README.md")
    res = run_gates(sandbox)
    assert res.returncode == 0, res.stderr
    assert sandbox.calls() == ["eval --replay"]


def test_gates_runs_live_only_for_gated_paths(sandbox):
    sandbox.stage("README.md")
    assert run_gates(sandbox).returncode == 0
    assert sandbox.calls() == ["eval --replay"]
    sandbox.log.unlink()
    sandbox.stage("agents/x.md")
    assert run_gates(sandbox).returncode == 0
    assert sandbox.calls() == ["eval --replay", "eval --live --if-reachable"]


def test_gates_fail_when_replay_fails(sandbox):
    (sandbox.fake_dir / "fail-replay").write_text("")
    sandbox.stage("agents/x.md")
    res = run_gates(sandbox)
    assert res.returncode != 0
    assert "eval --replay failed" in res.stderr and "boom-case" in res.stderr
    assert sandbox.calls() == ["eval --replay"]


def test_gates_fail_when_live_fails(sandbox):
    (sandbox.fake_dir / "fail-live").write_text("")
    sandbox.stage("agents/x.md")
    res = run_gates(sandbox)
    assert res.returncode != 0
    assert "eval --live failed" in res.stderr and "boom-case" in res.stderr


def test_gates_treat_non_json_output_as_failure(sandbox):
    Path(os.environ["CODE_CHECK_BIN"]).write_text("#!/bin/sh\necho oops not json\n")
    sandbox.stage("README.md")
    res = run_gates(sandbox)
    assert res.returncode != 0 and "oops not json" in res.stderr


def test_gates_skip_when_default_cli_is_missing(sandbox, monkeypatch):
    monkeypatch.delenv("CODE_CHECK_BIN")
    res = run_gates(sandbox)
    assert res.returncode == 0 and "skipping" in res.stderr and sandbox.calls() == []


def test_if_no_git_hook_exits_early_when_hookspath_set(sandbox):
    git(sandbox.repo, "config", "core.hooksPath", ".githooks")
    sandbox.stage("agents/x.md")
    res = run_gates(sandbox, "--if-no-git-hook")
    assert res.returncode == 0
    assert sandbox.calls() == []


def test_if_no_git_hook_runs_when_hookspath_unset(sandbox):
    sandbox.stage("README.md")
    assert run_gates(sandbox, "--if-no-git-hook").returncode == 0
    assert sandbox.calls() == ["eval --replay"]


def test_gates_run_without_flag_even_when_hookspath_set(sandbox):
    git(sandbox.repo, "config", "core.hooksPath", ".githooks")
    sandbox.stage("README.md")
    assert run_gates(sandbox).returncode == 0
    assert sandbox.calls() == ["eval --replay"]


def test_gate_paths_patterns_match_expected_files():
    patterns = [p for p in (REPO / "scripts/gate-paths.txt").read_text().splitlines() if p]
    assert patterns

    def matches(path):
        return any(
            subprocess.run(["grep", "-Eq", "--", p], input=path + "\n", text=True, check=False).returncode == 0
            for p in patterns
        )

    for p in patterns:  # valid ERE: grep exits 0/1, never 2
        assert subprocess.run(["grep", "-E", "--", p], input="", text=True, check=False).returncode in (0, 1)
    for path in (
        "src/codecheck/agentgen.py", "agents/skeptic.md", "skills/review-pr/SKILL.md", "src/codecheck/config.py",
        "src/codecheck/core/findings.py", "src/codecheck/core/diffmap.py", "src/codecheck/core/policy.py",
        "src/codecheck/commands/ingest.py", "src/codecheck/commands/post.py", "src/codecheck/evals/live.py",
    ):
        assert matches(path), path
    for path in ("README.md", "tests/test_gates.py", "src/codecheck/cli.py", "src/codecheck/commands/loop.py"):
        assert not matches(path), path
    for path in ("src/codecheck/core/findings.py", "src/codecheck/commands/ingest.py"):
        assert (REPO / path).exists(), path


def test_gates_work_with_git_env_exported(sandbox, tmp_path):
    sandbox.stage("agents/x.md")
    env = {
        **os.environ,
        "GIT_DIR": str(sandbox.repo / ".git"),
        "GIT_INDEX_FILE": str(sandbox.repo / ".git/index"),
    }
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    res = run_gates(sandbox, cwd=elsewhere, env=env)
    assert res.returncode == 0, res.stderr
    assert sandbox.calls() == ["eval --replay", "eval --live --if-reachable"]


def test_install_script_sets_hookspath_in_temp_repo_only(sandbox):
    before = subprocess.run(["git", "config", "core.hooksPath"], cwd=REPO, capture_output=True, text=True, check=False).stdout
    res = subprocess.run([str(sandbox.repo / "scripts/install-git-hooks.sh")], cwd=sandbox.repo, capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    assert git(sandbox.repo, "config", "core.hooksPath").stdout.strip() == ".githooks"
    again = subprocess.run([str(sandbox.repo / "scripts/install-git-hooks.sh")], cwd=sandbox.repo, capture_output=True, text=True, check=False)
    assert again.returncode == 0
    after = subprocess.run(["git", "config", "core.hooksPath"], cwd=REPO, capture_output=True, text=True, check=False).stdout
    assert before == after


def test_install_script_refuses_outside_git(tmp_path, git_env, monkeypatch):
    for var in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    plain = tmp_path / "plain"
    plain.mkdir()
    res = subprocess.run([str(REPO / "scripts/install-git-hooks.sh")], cwd=plain, capture_output=True, text=True, check=False)
    assert res.returncode != 0 and "not inside a git repository" in res.stderr


def test_precommit_hook_runs_gates(sandbox):
    git(sandbox.repo, "config", "core.hooksPath", ".githooks")
    (sandbox.repo / "README.md").write_text("hi\n")
    git(sandbox.repo, "add", "README.md")
    res = subprocess.run(["git", "commit", "-q", "-m", "one"], cwd=sandbox.repo, capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    assert "ruff not found" in res.stderr and "eslint not found" in res.stderr
    assert sandbox.calls() == ["eval --replay"]

    (sandbox.fake_dir / "fail-replay").write_text("")
    (sandbox.repo / "b.md").write_text("b\n")
    git(sandbox.repo, "add", "b.md")
    res = subprocess.run(["git", "commit", "-q", "-m", "two"], cwd=sandbox.repo, capture_output=True, text=True, check=False)
    assert res.returncode != 0 and "eval --replay failed" in res.stderr
    assert git(sandbox.repo, "rev-list", "--count", "HEAD").stdout.strip() == "1"


@pytest.mark.parametrize("script", ["scripts/gates.sh", "scripts/install-git-hooks.sh", ".githooks/pre-commit"])
def test_scripts_parse_and_are_executable(script):
    assert subprocess.run(["bash", "-n", str(REPO / script)], check=False).returncode == 0
    assert os.access(REPO / script, os.X_OK)
    if shutil.which("shellcheck"):
        assert subprocess.run(["shellcheck", str(REPO / script)], check=False).returncode == 0


def test_claude_settings_has_gates_hook():
    import json

    hooks = json.loads((REPO / ".claude/settings.json").read_text())["hooks"]["PreToolUse"][0]["hooks"]
    assert len(hooks) == 3
    last = hooks[2]
    assert last["if"] == "Bash(git commit*)" and last["timeout"] == 900
    assert "scripts/gates.sh" in last["command"] and '"$G" --if-no-git-hook' in last["command"]


def test_gates_work_when_repo_path_has_a_space(tmp_path, git_env, monkeypatch):
    for var in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE", "CODE_CHECK_BIN"):
        monkeypatch.delenv(var, raising=False)
    repo = tmp_path / "My Code" / "repo"
    (repo / "scripts").mkdir(parents=True)
    git(repo, "init", "-q")
    shutil.copy2(REPO / "scripts/gates.sh", repo / "scripts/gates.sh")
    shutil.copy2(REPO / "scripts/gate-paths.txt", repo / "scripts/gate-paths.txt")
    py = repo / ".venv/bin/python"
    py.parent.mkdir(parents=True)
    py.write_text('#!/bin/sh\necho "$@" >> "$FAKE_LOG"\necho \'{"passed": true}\'\n')
    py.chmod(0o755)
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "log"))
    res = subprocess.run([str(repo / "scripts/gates.sh")], cwd=repo, capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    assert (tmp_path / "log").read_text().split("\n")[0] == "-m codecheck eval --replay"


def _fake(sandbox, body):
    Path(os.environ["CODE_CHECK_BIN"]).write_text("#!/bin/sh\n" + body)


def test_gates_ignore_stderr_warning_on_passing_run(sandbox):
    _fake(sandbox, "echo 'warning: shadow recording failed: x' >&2\necho '{\"passed\": true}'\n")
    sandbox.stage("README.md")
    res = run_gates(sandbox)
    assert res.returncode == 0, res.stderr


def test_gates_show_stderr_on_failing_run(sandbox):
    _fake(sandbox, "echo 'warning: shadow recording failed: x' >&2\necho '{\"passed\": false}'\n")
    sandbox.stage("README.md")
    res = run_gates(sandbox)
    assert res.returncode != 0
    assert "shadow recording failed" in res.stderr and '"passed": false' in res.stderr


def _commit_base(box):
    box.stage("base.txt", "agents/x.md")
    git(box.repo, "commit", "-qm", "base", "--no-verify")
    box.log.unlink(missing_ok=True)


def test_claude_hook_mode_counts_unstaged_tracked_changes(sandbox):
    _commit_base(sandbox)
    (sandbox.repo / "agents/x.md").write_text("changed\n")
    assert run_gates(sandbox, "--if-no-git-hook").returncode == 0
    assert sandbox.calls() == ["eval --replay", "eval --live --if-reachable"]


def test_claude_hook_mode_counts_untracked_gated_files(sandbox):
    _commit_base(sandbox)
    (sandbox.repo / "skills").mkdir()
    (sandbox.repo / "skills/new.md").write_text("n\n")
    assert run_gates(sandbox, "--if-no-git-hook").returncode == 0
    assert sandbox.calls() == ["eval --replay", "eval --live --if-reachable"]


def test_normal_mode_ignores_unstaged_changes(sandbox):
    _commit_base(sandbox)
    (sandbox.repo / "agents/x.md").write_text("changed\n")
    (sandbox.repo / "skills").mkdir()
    (sandbox.repo / "skills/new.md").write_text("n\n")
    assert run_gates(sandbox).returncode == 0
    assert sandbox.calls() == ["eval --replay"]
    sandbox.log.unlink()
    git(sandbox.repo, "add", "agents/x.md")
    assert run_gates(sandbox).returncode == 0
    assert sandbox.calls() == ["eval --replay", "eval --live --if-reachable"]


def test_claude_hook_mode_without_head_does_not_crash(sandbox):
    (sandbox.repo / "README.md").write_text("r\n")
    res = run_gates(sandbox, "--if-no-git-hook")
    assert res.returncode == 0, res.stderr
    assert sandbox.calls() == ["eval --replay"]
    sandbox.log.unlink()
    (sandbox.repo / "agents").mkdir()
    (sandbox.repo / "agents/y.md").write_text("y\n")
    assert run_gates(sandbox, "--if-no-git-hook").returncode == 0
    assert sandbox.calls() == ["eval --replay", "eval --live --if-reachable"]
