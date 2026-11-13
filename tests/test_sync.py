import pytest

from codecheck.errors import SyncError
from codecheck.runner import SubprocessRunner
from codecheck.vcs.git import Git
from codecheck.vcs.sync import sync_base
from gitutil import commit_file, make_origin_and_clone, sh

pytestmark = pytest.mark.usefixtures("git_env")


def test_sync_fetches_latest_and_leaves_local_branches_alone(tmp_path):
    origin, clone = make_origin_and_clone(tmp_path)
    other = tmp_path / "other"
    sh(tmp_path, "clone", str(origin), str(other))
    commit_file(other, "n.txt", "n\n", "upstream change")
    sh(other, "push", "origin", "HEAD:main")
    local_main_before = sh(clone, "rev-parse", "main").strip()

    result = sync_base(Git(SubprocessRunner(), clone), "origin", "auto")

    assert result.fresh and result.warning is None
    assert result.base_ref == "origin/main"
    assert result.base_sha == sh(origin, "rev-parse", "main").strip()
    assert sh(clone, "rev-parse", "main").strip() == local_main_before  # not pulled


def test_sync_failure_marks_stale_instead_of_pretending(tmp_path):
    _, clone = make_origin_and_clone(tmp_path)
    sh(clone, "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"))
    result = sync_base(Git(SubprocessRunner(), clone), "origin", "main")
    assert not result.fresh
    assert "stale" in result.warning


def test_sync_without_any_base_ref_raises(tmp_path):
    sh(tmp_path, "init", "-b", "main", "lonely")
    with pytest.raises(SyncError):
        sync_base(Git(SubprocessRunner(), tmp_path / "lonely"), "origin", "main")


def test_sync_rejects_option_like_base_branch(tmp_path):
    _, clone = make_origin_and_clone(tmp_path)
    with pytest.raises(SyncError):
        sync_base(Git(SubprocessRunner(), clone), "origin", "--upload-pack=x")
