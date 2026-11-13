import argparse
import json
import os
import re
import stat
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

from codecheck import __version__, commands

REPO = Path(__file__).resolve().parent.parent
SKILLS = sorted((REPO / "skills").glob("*/SKILL.md"))
SUBCOMMANDS = {m.NAME for m in commands.ALL}
CLI_REF = re.compile(r"`code-check (?:--cwd \S+ )?([a-z][a-z-]*)")
NAMESPACED = re.compile(r"code-check:([a-z][a-z-]*)")


def frontmatter_of(path: Path) -> dict:
    return frontmatter(path.read_text())


SPAN = re.compile(r"`(code-check (?:--cwd \S+ )?[a-z][^`]*)`")


def _subparsers(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return {}


def invocation_errors(span: str) -> list[str]:
    """Check one backticked `code-check ...` invocation against the real argparse parser."""
    from codecheck.cli import build_parser

    parser = build_parser()
    tokens = span.split()[1:]
    if tokens[:1] == ["--cwd"]:
        tokens = tokens[2:]
    subs = _subparsers(parser)
    if not tokens or tokens[0] not in subs:
        return [f"unknown subcommand in `{span}`"]
    current = subs[tokens[0]]
    rest = tokens[1:]
    nested = _subparsers(current)
    if nested and rest and rest[0] in nested:
        current, rest = nested[rest[0]], rest[1:]
    known = {opt for a in current._actions for opt in a.option_strings}
    return [f"unknown flag {t} in `{span}`" for t in rest if t.startswith("--") and t not in known]


def frontmatter(text: str) -> dict:
    head = text.split("---\n")[1]
    return {k.strip(): v.strip().strip('"') for k, v in (line.split(":", 1) for line in head.strip().splitlines())}


AGENT_NAMES = {frontmatter_of(p)["name"] for p in (REPO / "agents").glob("*.md")}


def test_exactly_the_four_skills():
    assert {p.parent.name for p in SKILLS} == {"review-local", "review-pr", "address-comments", "babysit-pr"}


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_skill_frontmatter_is_valid(path):
    fm = frontmatter(path.read_text())
    assert fm["name"] == path.parent.name
    assert 40 <= len(fm["description"]) <= 1024


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_skill_only_references_real_subcommands_and_agents(path):
    text = path.read_text()
    used = set(CLI_REF.findall(text))
    assert used, "a skill that never calls the CLI cannot be deterministic"
    assert used <= SUBCOMMANDS, f"unknown subcommands: {used - SUBCOMMANDS}"
    known = AGENT_NAMES | {p.parent.name for p in SKILLS}
    assert set(NAMESPACED.findall(text)) <= known
    assert "${CLAUDE_PLUGIN_ROOT}/bin/code-check" in text


def test_review_local_never_posts_and_no_skill_instructs_a_force_push():
    local = (REPO / "skills/review-local/SKILL.md").read_text()
    assert "never post" in local.lower()
    for path in SKILLS:
        text = path.read_text()
        assert "git push --force" not in text and "push -f" not in text and "--force-with-lease" not in text


def test_versions_agree_everywhere():
    version = (REPO / "VERSION").read_text().strip()
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    plugin = json.loads((REPO / ".claude-plugin/plugin.json").read_text())
    market = json.loads((REPO / ".claude-plugin/marketplace.json").read_text())
    package = json.loads((REPO / "package.json").read_text())
    assert {version, pyproject, __version__, plugin["version"], package["version"],
            market["plugins"][0]["version"]} == {version}


def test_manifests():
    plugin = json.loads((REPO / ".claude-plugin/plugin.json").read_text())
    market = json.loads((REPO / ".claude-plugin/marketplace.json").read_text())
    assert plugin["name"] == "code-check" and plugin["description"]
    assert market["name"] == "code-check" and market["plugins"][0]["name"] == "code-check"
    assert market["plugins"][0]["source"] == "./"


def test_session_start_hook_points_at_an_executable_script():
    hooks = json.loads((REPO / "hooks/hooks.json").read_text())["hooks"]["SessionStart"]
    cmd = hooks[0]["hooks"][0]["command"]
    assert cmd == "${CLAUDE_PLUGIN_ROOT}/hooks/ensure-runtime.sh"
    assert os.access(REPO / "hooks/ensure-runtime.sh", os.X_OK) and os.access(REPO / "bin/code-check", os.X_OK)


@pytest.mark.parametrize("script", ["bin/code-check", "hooks/ensure-runtime.sh", "hooks/common.sh"])
def test_shell_scripts_parse(script):
    assert subprocess.run(["sh", "-n", str(REPO / script)]).returncode == 0


def test_wrapper_runs_the_cli_with_an_overridden_python(tmp_path):
    env = {**os.environ, "CODE_CHECK_PYTHON": sys.executable, "PYTHONPATH": str(REPO / "src")}
    res = subprocess.run([str(REPO / "bin/code-check"), "--version"], env=env, capture_output=True, text=True)
    assert res.returncode == 0 and res.stdout.strip() == f"code-check {__version__}"


def test_ensure_runtime_is_a_noop_when_current(tmp_path):
    py = tmp_path / "venv/bin/python"
    py.parent.mkdir(parents=True)
    py.write_text("#!/bin/sh\nexit 0\n")
    py.chmod(py.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "installed-version").write_text((REPO / "VERSION").read_text().strip())
    env = {**os.environ, "CODE_CHECK_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    res = subprocess.run([str(REPO / "hooks/ensure-runtime.sh")], env=env, capture_output=True, text=True)
    assert res.returncode == 0 and res.stdout == "" and res.stderr == ""


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_every_invocation_in_a_skill_uses_real_flags(path):
    spans = SPAN.findall(path.read_text())
    assert spans
    errors = [e for span in spans for e in invocation_errors(span)]
    assert not errors, errors


def test_invocation_check_detects_bogus_references():
    assert invocation_errors("code-check frobnicate --x")
    assert invocation_errors("code-check precheck --mode pr --bogus")
    assert invocation_errors("code-check shadow record-verdict --nope")
    assert not invocation_errors("code-check shadow record-verdict --run <id>")
    assert not invocation_errors("code-check --cwd <WS> commit --message m --paths a")


def test_agent_names_come_from_frontmatter_and_cover_all_languages():
    from codecheck.core.panel import CORE_REVIEWERS, LANGUAGES

    assert {p.stem for p in (REPO / "agents").glob("*.md")} == AGENT_NAMES
    assert {f"lang-{n}" for n in LANGUAGES} <= AGENT_NAMES
    assert len(LANGUAGES) == 15
    assert set(CORE_REVIEWERS) <= AGENT_NAMES and "aggregator" in AGENT_NAMES


# --- bootstrap behaviour, with a stub uv on PATH (no network, no real installs) ---

def _stub_uv(bindir: Path) -> None:
    bindir.mkdir(exist_ok=True)
    uv = bindir / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'echo "$@" >> "$STUB_LOG"\n'
        'if [ "$1" = venv ]; then\n'
        '  for a; do last="$a"; done\n'
        '  mkdir -p "$last/bin"\n'
        '  printf \'#!/bin/sh\\necho ran "$@"\\n\' > "$last/bin/python"\n'
        '  chmod +x "$last/bin/python"\n'
        'elif [ "$1" = pip ]; then\n'
        '  [ -z "${STUB_SLEEP:-}" ] || sleep "$STUB_SLEEP"\n'
        '  [ -z "${STUB_STEAL_LOCK:-}" ] || printf someone-else > "$STUB_STEAL_LOCK"\n'
        '  [ -z "${STUB_FAIL:-}" ] || exit 1\n'
        "fi\n"
    )
    uv.chmod(0o755)


def _env(tmp_path, **extra):
    env = {k: v for k, v in os.environ.items() if k not in ("CODE_CHECK_PYTHON", "CLAUDE_PLUGIN_DATA")}
    env.update(CODE_CHECK_HOME=str(tmp_path / "data"), PATH=f"{tmp_path / 'bin'}:/usr/bin:/bin",
               STUB_LOG=str(tmp_path / "log"), **extra)
    return env


def test_failed_install_leaves_no_venv_and_is_retried(tmp_path):
    _stub_uv(tmp_path / "bin")
    script = str(REPO / "hooks/ensure-runtime.sh")
    bad = subprocess.run([script], env=_env(tmp_path, STUB_FAIL="1"), capture_output=True, text=True)
    assert bad.returncode != 0
    data = tmp_path / "data"
    assert not (data / "venv").exists() and not (data / "installed-version").exists()
    assert not (data / ".lock").exists()
    good = subprocess.run([script], env=_env(tmp_path), capture_output=True, text=True)
    assert good.returncode == 0
    assert (data / "venv/bin/python").exists()
    assert (data / "installed-version").read_text() == (REPO / "VERSION").read_text().strip()


def test_wrapper_bootstraps_itself_and_rebootstraps_after_version_change(tmp_path):
    _stub_uv(tmp_path / "bin")
    env = _env(tmp_path)
    first = subprocess.run([str(REPO / "bin/code-check"), "x"], env=env, capture_output=True, text=True)
    assert first.returncode == 0 and first.stdout.strip() == "ran -m codecheck x"
    installs = (tmp_path / "log").read_text().count("pip install")
    assert installs == 1
    subprocess.run([str(REPO / "bin/code-check"), "y"], env=env, capture_output=True, text=True)
    assert (tmp_path / "log").read_text().count("pip install") == 1
    (tmp_path / "data/installed-version").write_text("0.0.0-old")
    subprocess.run([str(REPO / "bin/code-check"), "z"], env=env, capture_output=True, text=True)
    assert (tmp_path / "log").read_text().count("pip install") == 2


def _make_lock(tmp_path, owner, beat):
    lock = tmp_path / "data/.lock"
    lock.mkdir(parents=True)
    (lock / "owner").write_text(owner)
    (lock / "beat").write_text(str(beat))
    return lock


def test_stale_lock_is_broken(tmp_path):
    _stub_uv(tmp_path / "bin")
    _make_lock(tmp_path, "dead-1", 1_000_000_000)
    res = subprocess.run([str(REPO / "hooks/ensure-runtime.sh")], env=_env(tmp_path), capture_output=True, text=True)
    assert res.returncode == 0 and (tmp_path / "data/venv/bin/python").exists()


def test_simultaneous_stale_breakers_bootstrap_once(tmp_path):
    _stub_uv(tmp_path / "bin")
    _make_lock(tmp_path, "dead-1", 1_000_000_000)
    env = _env(tmp_path, STUB_SLEEP="1")
    procs = [subprocess.Popen([str(REPO / "hooks/ensure-runtime.sh")], env=env) for _ in range(5)]
    assert [p.wait(timeout=60) for p in procs] == [0] * 5
    assert (tmp_path / "log").read_text().count("pip install") == 1


def test_live_holder_past_the_stale_threshold_is_not_broken(tmp_path):
    _stub_uv(tmp_path / "bin")
    # install takes ~6s, stale threshold is 3s: the heartbeat must keep the lock alive
    env = _env(tmp_path, STUB_SLEEP="6", CODE_CHECK_LOCK_STALE_SECS="3")
    procs = [subprocess.Popen([str(REPO / "hooks/ensure-runtime.sh")], env=env)]
    time.sleep(1)
    procs.append(subprocess.Popen([str(REPO / "hooks/ensure-runtime.sh")], env=env))
    assert [p.wait(timeout=60) for p in procs] == [0, 0]
    assert (tmp_path / "log").read_text().count("pip install") == 1


def test_failed_rebuild_keeps_the_previous_venv_and_marker(tmp_path):
    _stub_uv(tmp_path / "bin")
    script = str(REPO / "hooks/ensure-runtime.sh")
    assert subprocess.run([script], env=_env(tmp_path)).returncode == 0
    data = tmp_path / "data"
    (data / "installed-version").write_text("0.0.0-old")
    py_before = (data / "venv/bin/python").read_text()
    bad = subprocess.run([script], env=_env(tmp_path, STUB_FAIL="1"), capture_output=True, text=True)
    assert bad.returncode != 0
    assert (data / "venv/bin/python").read_text() == py_before
    assert (data / "installed-version").read_text() == "0.0.0-old"
    assert not (data / ".lock").exists()
    assert [p.name for p in data.iterdir() if ".new." in p.name or ".old." in p.name] == []


def test_cleanup_does_not_remove_another_owners_lock(tmp_path):
    _stub_uv(tmp_path / "bin")
    # the stub steals the lock mid-install by replacing the owner token
    env = _env(tmp_path, STUB_STEAL_LOCK=str(tmp_path / "data/.lock/owner"))
    res = subprocess.run([str(REPO / "hooks/ensure-runtime.sh")], env=env, capture_output=True, text=True)
    assert res.returncode != 0 and "lost the install lock" in res.stderr
    assert (tmp_path / "data/.lock/owner").read_text() == "someone-else"
    assert not (tmp_path / "data/venv").exists()


def test_unwritable_data_dir_fails_fast(tmp_path):
    _stub_uv(tmp_path / "bin")
    data = tmp_path / "data"
    data.mkdir()
    data.chmod(0o555)
    try:
        res = subprocess.run([str(REPO / "hooks/ensure-runtime.sh")], env=_env(tmp_path), capture_output=True,
                             text=True, timeout=20)
    finally:
        data.chmod(0o755)
    assert res.returncode != 0 and "lock" in res.stderr


def test_missing_home_and_data_fails_clearly(tmp_path):
    env = {"PATH": "/usr/bin:/bin"}
    res = subprocess.run([str(REPO / "bin/code-check"), "--version"], env=env, capture_output=True, text=True)
    assert res.returncode != 0 and "HOME" in res.stderr


def test_concurrent_bootstraps_install_once(tmp_path):
    _stub_uv(tmp_path / "bin")
    env = _env(tmp_path)
    procs = [subprocess.Popen([str(REPO / "hooks/ensure-runtime.sh")], env=env) for _ in range(4)]
    assert [p.wait(timeout=60) for p in procs] == [0, 0, 0, 0]
    assert (tmp_path / "log").read_text().count("pip install") == 1


def _wait_for(pred, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.2)
    return False


def test_killed_holder_heartbeat_stops_and_next_run_recovers(tmp_path):
    _stub_uv(tmp_path / "bin")
    env = _env(tmp_path, STUB_SLEEP="40", CODE_CHECK_LOCK_STALE_SECS="3")
    data = tmp_path / "data"
    proc = subprocess.Popen([str(REPO / "hooks/ensure-runtime.sh")], env=env)
    assert _wait_for(lambda: list(data.glob("venv.new.*")) and (data / ".lock/beat").exists())
    proc.kill()
    proc.wait()
    time.sleep(3)  # heartbeat interval is 1s; it must notice the parent is gone
    beat = (data / ".lock/beat").read_text()
    time.sleep(2.5)
    assert (data / ".lock/beat").read_text() == beat, "heartbeat kept a dead holder's lock fresh"
    env2 = _env(tmp_path, CODE_CHECK_LOCK_STALE_SECS="3")
    res = subprocess.run([str(REPO / "hooks/ensure-runtime.sh")], env=env2, capture_output=True, text=True, timeout=60)
    assert res.returncode == 0 and (data / "venv/bin/python").exists()
    assert list(data.glob("venv.new.*")) == []


def test_dead_break_mutex_does_not_hang_the_waiter(tmp_path):
    _stub_uv(tmp_path / "bin")
    lock = _make_lock(tmp_path, "dead-1", 1_000_000_000)
    (tmp_path / "data/.lock.break").mkdir()  # breaker died before doing anything
    env = _env(tmp_path, CODE_CHECK_LOCK_STALE_SECS="3", CODE_CHECK_BREAK_STALE_SECS="2")
    res = subprocess.run([str(REPO / "hooks/ensure-runtime.sh")], env=env, capture_output=True, text=True, timeout=60)
    assert res.returncode == 0 and (tmp_path / "data/venv/bin/python").exists()
    assert not lock.exists() and not (tmp_path / "data/.lock.break").exists()


def test_wait_timeout_is_reachable_with_a_wedged_mutex(tmp_path):
    _stub_uv(tmp_path / "bin")
    _make_lock(tmp_path, "dead-1", 1_000_000_000)
    (tmp_path / "data/.lock.break").mkdir()
    env = _env(tmp_path, CODE_CHECK_LOCK_STALE_SECS="1", CODE_CHECK_BREAK_STALE_SECS="1000",
               CODE_CHECK_WAIT_MARGIN_SECS="4")
    res = subprocess.run([str(REPO / "hooks/ensure-runtime.sh")], env=env, capture_output=True, text=True, timeout=120)
    assert res.returncode != 0 and "timed out" in res.stderr


def test_orphaned_previous_venv_is_restored_when_live_venv_is_missing(tmp_path):
    _stub_uv(tmp_path / "bin")
    data = tmp_path / "data"
    (data / "venv.old.999/bin").mkdir(parents=True)
    (data / "venv.old.999/bin/python").write_text("#!/bin/sh\necho old\n")
    (data / "venv.old.999/bin/python").chmod(0o755)
    env = _env(tmp_path, STUB_FAIL="1")
    assert subprocess.run([str(REPO / "hooks/ensure-runtime.sh")], env=env, capture_output=True).returncode != 0
    assert "echo old" in (data / "venv/bin/python").read_text()


def test_long_running_commands_carry_bash_timeout_guidance():
    # A foreground Bash call is killed after minutes; a loop iteration or a test-gated push can take longer.
    for skill in ("review-pr", "babysit-pr"):
        text = (REPO / f"skills/{skill}/SKILL.md").read_text()
        assert "run_in_background" in text and "code-check loop --mode" in text
    for skill in ("address-comments", "babysit-pr"):
        text = (REPO / f"skills/{skill}/SKILL.md").read_text()
        assert "600000" in text and "push --pr" in text
    assert "code-check cleanup --run" in (REPO / "skills/review-local/SKILL.md").read_text()
