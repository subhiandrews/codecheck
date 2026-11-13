import os

import pytest
from gitutil import make_origin_and_clone, sh

from codecheck.core.guidance import Guidance, from_base, from_worktree
from codecheck.runner import SubprocessRunner
from codecheck.vcs.git import Git

pytestmark = pytest.mark.usefixtures("git_env")


def _sub(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    return d


def test_from_worktree_reads_file(tmp_path):
    (tmp_path / "CODE_CHECK.md").write_text("look at X\n")
    g = from_worktree(tmp_path)
    assert g == Guidance("worktree", "look at X\n", False)
    assert g.to_meta() == {"source": "worktree", "truncated": False}


def test_missing_returns_none(tmp_path):
    assert from_worktree(tmp_path) is None


def test_whitespace_only_and_non_utf8_are_absent(tmp_path):
    (tmp_path / "CODE_CHECK.md").write_text("  \n\t")
    assert from_worktree(tmp_path) is None
    (tmp_path / "CODE_CHECK.md").write_bytes(b"\xff\xfe\x00")
    assert from_worktree(tmp_path) is None


def test_symlink_is_refused(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("SECRET")
    os.symlink(tmp_path / "secret.txt", root / "CODE_CHECK.md")
    assert from_worktree(root) is None


def test_truncation(tmp_path):
    (tmp_path / "CODE_CHECK.md").write_text("a" * 20_000)
    g = from_worktree(tmp_path)
    assert g.truncated is True
    assert g.text[:16_000] == "a" * 16_000
    assert g.text[16_000:] == "\n[CODE_CHECK.md truncated at 16000 characters]"
    assert len(g.text) < 16_200


def test_from_base_reads_regular_blob_and_refuses_symlink_blob(tmp_path):
    _, clone = make_origin_and_clone(_sub(tmp_path, "one"))
    (clone / "CODE_CHECK.md").write_text("be careful\n")
    sh(clone, "add", "CODE_CHECK.md")
    sh(clone, "commit", "-m", "guidance")
    git = Git(SubprocessRunner(), clone)
    assert from_base(git, "HEAD") == Guidance("base", "be careful\n", False)
    assert from_base(git, "no-such-ref") is None

    _, clone2 = make_origin_and_clone(_sub(tmp_path, "two"))
    (clone2 / "target.txt").write_text("SECRET")
    os.symlink("target.txt", clone2 / "CODE_CHECK.md")
    sh(clone2, "add", "CODE_CHECK.md", "target.txt")
    sh(clone2, "commit", "-m", "symlink")
    assert from_base(Git(SubprocessRunner(), clone2), "HEAD") is None
