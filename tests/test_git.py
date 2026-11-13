import pytest

from codecheck.errors import GitError
from codecheck.runner import SubprocessRunner
from codecheck.vcs.git import Git
from fakes import FakeRunner
from gitutil import commit_file, make_origin_and_clone, sh

pytestmark = pytest.mark.usefixtures("git_env")


def git_for(path):
    return Git(SubprocessRunner(), path)


def test_default_branch_and_ref_exists(tmp_path):
    _, clone = make_origin_and_clone(tmp_path)
    g = git_for(clone)
    assert g.default_branch("origin") == "main"
    assert g.ref_exists("origin/main") and not g.ref_exists("origin/nope")


def test_local_diff_includes_committed_uncommitted_and_untracked(tmp_path):
    _, clone = make_origin_and_clone(tmp_path)
    g = git_for(clone)
    base = g.rev_parse("origin/main")
    sh(clone, "checkout", "-b", "feat")
    commit_file(clone, "a.txt", "one\n", "add a")
    (clone / "README.md").write_text("hello\nedited\n")  # uncommitted
    (clone / "new file.txt").write_text("brand new\n")  # untracked, with a space
    diff = g.local_diff(base)
    assert "diff --git a/a.txt b/a.txt" in diff
    assert "+edited" in diff
    assert "new file.txt" in diff and "+brand new" in diff


def test_is_dirty_and_untracked(tmp_path):
    _, clone = make_origin_and_clone(tmp_path)
    g = git_for(clone)
    assert not g.is_dirty()
    (clone / "x").write_text("1")
    assert g.is_dirty() and g.untracked() == ["x"]


def test_behind_count_and_merge_base(tmp_path):
    origin, clone = make_origin_and_clone(tmp_path)
    g = git_for(clone)
    other = tmp_path / "other"
    sh(tmp_path, "clone", str(origin), str(other))
    commit_file(other, "n.txt", "n\n", "upstream change")
    sh(other, "push", "origin", "HEAD:main")
    assert g.fetch("origin", "main") is True
    assert g.behind_count("origin/main") == 1
    assert g.merge_base("origin/main", "HEAD") == g.head_sha()


def test_commit_requires_paths_and_returns_sha(tmp_path):
    _, clone = make_origin_and_clone(tmp_path)
    g = git_for(clone)
    with pytest.raises(GitError):
        g.commit("msg", [])
    (clone / "f.txt").write_text("f")
    sha = g.commit("add f", ["f.txt"])
    assert sha == g.head_sha()


def test_push_never_forces():
    runner = FakeRunner().on("git", "-c", "core.quotepath=false", "push", "origin", "HEAD:refs/heads/feat")
    Git(runner, "/x").push("origin", "feat")
    argv = runner.argvs()[0]
    assert not any(a in ("-f", "--force", "--force-with-lease") or a.startswith("+") for a in argv)


def test_push_failure_raises_git_error():
    runner = FakeRunner().on(
        "git", "-c", "core.quotepath=false", "push", "origin", "HEAD:refs/heads/feat",
        returncode=1, stderr="! [rejected] (non-fast-forward)",
    )
    with pytest.raises(GitError, match="non-fast-forward"):
        Git(runner, "/x").push("origin", "feat")


def test_option_like_refs_are_rejected(tmp_path):
    _, clone = make_origin_and_clone(tmp_path)
    g = git_for(clone)
    bad = "--upload-pack=x"
    for call in (
        lambda: g.fetch("origin", bad), lambda: g.fetch(bad), lambda: g.ref_exists(bad),
        lambda: g.rev_parse(bad), lambda: g.merge_base(bad, "HEAD"), lambda: g.diff(bad),
        lambda: g.behind_count(bad), lambda: g.remote_url(bad), lambda: g.push("origin", bad),
        lambda: g.commit_time(bad), lambda: g.local_diff(bad), lambda: g.worktree_add(tmp_path / "w", bad),
    ):
        with pytest.raises(GitError, match="starts with '-'"):
            call()


def test_commit_leaves_other_staged_files_out(tmp_path):
    _, clone = make_origin_and_clone(tmp_path)
    g = git_for(clone)
    (clone / "other.txt").write_text("o")
    sh(clone, "add", "other.txt")
    (clone / "mine.txt").write_text("m")
    sha = g.commit("mine only", ["mine.txt"])
    assert sh(clone, "show", "--name-only", "--format=", sha).split() == ["mine.txt"]
    assert "other.txt" in sh(clone, "diff", "--cached", "--name-only")


def test_worktree_add_and_remove(tmp_path):
    _, clone = make_origin_and_clone(tmp_path)
    g = git_for(clone)
    wt = tmp_path / "wt"
    g.worktree_add(wt, "origin/main")
    assert (wt / "README.md").exists()
    g.worktree_remove(wt)
    assert not wt.exists()
    with pytest.raises(GitError):
        g.worktree_remove(wt)
