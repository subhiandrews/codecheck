import os
import re

import pytest

from codecheck.shadow.buildid import plugin_build


def _tree(tmp_path):
    (tmp_path / "agents").mkdir()
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text("{}")
    (tmp_path / "skills" / "s").mkdir(parents=True)
    (tmp_path / "agents" / "a.md").write_text("agent")
    (tmp_path / "skills" / "s" / "SKILL.md").write_text("skill")
    return tmp_path


def _hash(root):
    return plugin_build({"CLAUDE_PLUGIN_ROOT": str(root)})["prompts_hash"]


def test_prompts_hash_changes_when_an_agent_file_changes(tmp_path):
    tree = _tree(tmp_path)
    before = _hash(tree)
    (tree / "agents" / "a.md").write_text("agent changed")
    after = _hash(tree)
    assert before != after and re.fullmatch(r"[0-9a-f]{12}", after)


def test_prompts_hash_ignores_other_files_and_symlinks(tmp_path):
    tree = _tree(tmp_path)
    before = _hash(tree)
    (tree / "README.md").write_text("x")
    (tree / "agents" / "link.md").symlink_to(tree / "agents" / "a.md")
    (tree / "agents" / "notes.txt").write_text("x")
    assert _hash(tree) == before


def test_plugin_build_uses_claude_plugin_root_first(tmp_path):
    tree = _tree(tmp_path)
    res = plugin_build({"CLAUDE_PLUGIN_ROOT": str(tree)})
    assert res["plugin_root"] == str(tree.resolve()) and res["prompts_hash"]


def test_plugin_build_without_a_usable_root_reports_a_reason(tmp_path):
    for env in ({}, {"CLAUDE_PLUGIN_ROOT": str(tmp_path / "missing")}):
        res = plugin_build(env, default_root=None)
        assert res["plugin_root"] is None and res["prompts_hash"] is None and res["build_reason"]


def test_code_check_plugin_root_wins_over_claude_plugin_root(tmp_path):
    (tmp_path / "mine").mkdir()
    (tmp_path / "other").mkdir()
    mine, other = _tree(tmp_path / "mine"), _tree(tmp_path / "other")
    (other / "agents" / "a.md").write_text("different")
    res = plugin_build({"CODE_CHECK_PLUGIN_ROOT": str(mine), "CLAUDE_PLUGIN_ROOT": str(other)}, default_root=None)
    assert res["plugin_root"] == str(mine.resolve()) and res["prompts_hash"] == _hash(mine)


def test_configured_root_must_look_like_a_plugin_root(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    for var in ("CODE_CHECK_PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"):
        res = plugin_build({var: str(empty)}, default_root=None)
        assert res["plugin_root"] is None and res["prompts_hash"] is None and var in res["build_reason"]


def test_plugin_build_never_raises_on_a_non_utf8_filename(tmp_path):
    tree = _tree(tmp_path)
    try:
        with open(os.path.join(os.fsencode(tree / "agents"), b"bad\xff.md"), "w") as f:
            f.write("x")
    except OSError:
        pytest.skip("filesystem refuses non-UTF-8 names")
    res = plugin_build({"CLAUDE_PLUGIN_ROOT": str(tree)}, default_root=None)
    assert set(res) == {"plugin_root", "prompts_hash", "build_reason"}
