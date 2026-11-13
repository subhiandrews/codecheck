import json
import os
import re
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from codecheck import __version__, commands

REPO = Path(__file__).resolve().parent.parent
SKILLS = sorted((REPO / "skills").glob("*/SKILL.md"))
SUBCOMMANDS = {m.NAME for m in commands.ALL}
CLI_REF = re.compile(r"`code-check (?:--cwd \S+ )?([a-z][a-z-]*)")
NAMESPACED = re.compile(r"code-check:([a-z][a-z-]*)")


def frontmatter(text: str) -> dict:
    head = text.split("---\n")[1]
    return {k.strip(): v.strip().strip('"') for k, v in (line.split(":", 1) for line in head.strip().splitlines())}


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
    known = {p.stem for p in (REPO / "agents").glob("*.md")} | {p.parent.name for p in SKILLS} | {"member", "language"}
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


@pytest.mark.parametrize("script", ["bin/code-check", "hooks/ensure-runtime.sh"])
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
