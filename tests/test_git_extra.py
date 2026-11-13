import subprocess

import pytest
from gitutil import commit_file, make_origin_and_clone, sh

from codecheck.errors import GitError
from codecheck.runner import SubprocessRunner
from codecheck.vcs.git import Git

pytestmark = pytest.mark.usefixtures("git_env")


def test_is_ancestor(tmp_path):
    _, clone = make_origin_and_clone(tmp_path)
    g = Git(SubprocessRunner(), clone)
    first = g.head_sha()
    commit_file(clone, "a.txt", "a\n", "second")
    assert g.is_ancestor(first, "HEAD") and not g.is_ancestor("HEAD", first)


def test_worktree_add_branch_creates_a_real_branch(tmp_path):
    _, clone = make_origin_and_clone(tmp_path)
    g = Git(SubprocessRunner(), clone)
    wt = tmp_path / "wt"
    g.worktree_add_branch(wt, "code-check/pr-7", "HEAD")
    assert Git(SubprocessRunner(), wt).current_branch() == "code-check/pr-7"


def test_would_conflict(tmp_path):
    origin, clone = make_origin_and_clone(tmp_path)
    if subprocess.run(["git", "merge-tree", "--write-tree", "HEAD", "HEAD"], cwd=clone, capture_output=True, check=False).returncode != 0:
        pytest.skip("git too old for merge-tree --write-tree")
    g = Git(SubprocessRunner(), clone)
    sh(clone, "checkout", "-b", "feat")
    commit_file(clone, "README.md", "mine\n", "mine")
    other = tmp_path / "other"
    sh(tmp_path, "clone", str(origin), str(other))
    commit_file(other, "README.md", "theirs\n", "theirs")
    sh(other, "push", "origin", "HEAD:main")
    g.fetch("origin", "main")
    assert g.would_conflict("origin/main") is True
    commit_file(other, "other.txt", "x\n", "unrelated")
    sh(other, "push", "origin", "HEAD:main")


def test_show_file_rejects_option_like_ref(tmp_path):
    _, clone = make_origin_and_clone(tmp_path)
    with pytest.raises(GitError):
        Git(SubprocessRunner(), clone).show_file("--output=x", "CODE_CHECK.md")
