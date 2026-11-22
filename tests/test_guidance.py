import os

import pytest
from gitutil import make_origin_and_clone, sh

from codecheck.core.guidance import (
    MAX_GUIDANCE_CHARS,
    MAX_GUIDANCE_DIRS,
    Guidance,
    from_base,
    from_worktree,
    guidance_dirs,
)
from codecheck.errors import GitError
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
    assert g.to_meta() == {"source": "worktree", "truncated": False, "files": ["CODE_CHECK.md"], "skipped": []}


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


def test_from_base_non_utf8_blob_is_absent(tmp_path):
    _, clone = make_origin_and_clone(_sub(tmp_path, "bad"))
    (clone / "CODE_CHECK.md").write_bytes(b"\xff\xfe\x00bad")
    sh(clone, "add", "CODE_CHECK.md")
    sh(clone, "commit", "-m", "binary guidance")
    assert from_base(Git(SubprocessRunner(), clone), "HEAD") is None


API_HEADER = "[services/api/CODE_CHECK.md: the rules below apply only to files under `services/api/`; for those files they win over any rule above that they conflict with]\n"


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_guidance_dirs_are_the_ancestors_shallowest_first_and_unsafe_paths_give_none():
    assert guidance_dirs(["services/api/src/x.py", "web/y.js", "top.py"]) == ["services", "web", "services/api", "services/api/src"]
    assert guidance_dirs(["top.py", None, 3]) == []
    assert guidance_dirs(["/etc/x/y", "a/../b/c.py", "./a/b.py", "a//b.py", "a\nb/c.py", "a`b/c.py"]) == []


def test_worktree_nested_file_applies_only_above_a_changed_path(tmp_path):
    _write(tmp_path, "CODE_CHECK.md", "root rules\n")
    _write(tmp_path, "services/api/CODE_CHECK.md", "api rules\n")
    _write(tmp_path, "web/CODE_CHECK.md", "web rules\n")
    g = from_worktree(tmp_path, ["services/api/src/x.py"])
    assert g.text == "root rules\n\n" + API_HEADER + "api rules\n"
    assert g.files == ("CODE_CHECK.md", "services/api/CODE_CHECK.md") and g.truncated is False
    assert from_worktree(tmp_path, ["docs/readme.md"]) == Guidance("worktree", "root rules\n", False)
    assert from_worktree(tmp_path) == Guidance("worktree", "root rules\n", False)


def test_worktree_nested_file_without_a_root_file_and_root_without_trailing_newline(tmp_path):
    _write(tmp_path, "services/api/CODE_CHECK.md", "api rules\n")
    g = from_worktree(tmp_path, ["services/api/x.py"])
    assert g.text == API_HEADER + "api rules\n" and g.files == ("services/api/CODE_CHECK.md",)
    _write(tmp_path, "CODE_CHECK.md", "root rules")
    assert from_worktree(tmp_path, ["services/api/x.py"]).text == "root rules\n\n" + API_HEADER + "api rules\n"


def test_worktree_nested_file_behind_a_symlinked_directory_is_refused(tmp_path):
    root = tmp_path / "repo"
    _write(tmp_path, "outside/api/CODE_CHECK.md", "SECRET\n")
    _write(root, "CODE_CHECK.md", "root rules\n")
    os.symlink(tmp_path / "outside", root / "services")
    assert from_worktree(root, ["services/api/x.py"]) == Guidance("worktree", "root rules\n", False)


def test_a_large_root_file_is_cut_to_leave_room_for_the_nested_ones(tmp_path):
    _write(tmp_path, "CODE_CHECK.md", "r" * (MAX_GUIDANCE_CHARS + 500))
    _write(tmp_path, "a/CODE_CHECK.md", "n" * 500)
    _write(tmp_path, "a/b/CODE_CHECK.md", "deep rules\n")
    g = from_worktree(tmp_path, ["a/b/x.py"])
    marker = "\n[CODE_CHECK.md truncated at 16000 characters]"
    assert g.truncated is True and g.text.endswith(marker)
    assert len(g.text) <= MAX_GUIDANCE_CHARS + len(marker)
    assert g.files == ("CODE_CHECK.md", "a/CODE_CHECK.md", "a/b/CODE_CHECK.md") and g.skipped == ()
    assert "n" * 500 in g.text and "deep rules\n" in g.text
    assert "r" * 100 in g.text and "r" * MAX_GUIDANCE_CHARS not in g.text


def test_a_nested_file_with_no_room_is_named_as_skipped_and_never_half_listed(tmp_path):
    _write(tmp_path, "CODE_CHECK.md", "root\n")
    _write(tmp_path, "a/CODE_CHECK.md", "x" * (MAX_GUIDANCE_CHARS - 250))
    _write(tmp_path, "a/b/CODE_CHECK.md", "deep rules\n")
    g = from_worktree(tmp_path, ["a/b/x.py"])
    assert g.truncated is True and g.skipped == ("a/b/CODE_CHECK.md",)
    assert g.files == ("CODE_CHECK.md", "a/CODE_CHECK.md")
    assert "a/b/CODE_CHECK.md" not in g.text.replace("truncated", "") and "deep rules" not in g.text


def test_a_nested_file_that_does_not_fit_is_cut_only_when_enough_of_it_survives(tmp_path):
    _write(tmp_path, "CODE_CHECK.md", "r" * (MAX_GUIDANCE_CHARS - 10))
    _write(tmp_path, "a/CODE_CHECK.md", "n" * 8000)
    g = from_worktree(tmp_path, ["a/x.py"])
    assert g.truncated is True and g.files == ("CODE_CHECK.md", "a/CODE_CHECK.md") and g.skipped == ()
    assert "[a/CODE_CHECK.md: the rules below apply only" in g.text and g.text.count("n") >= 200


def test_from_base_reads_nested_blobs_above_changed_paths_only(tmp_path):
    _, clone = make_origin_and_clone(_sub(tmp_path, "nested"))
    _write(clone, "CODE_CHECK.md", "root rules\n")
    _write(clone, "services/api/CODE_CHECK.md", "api rules\n")
    _write(clone, "web/CODE_CHECK.md", "web rules\n")
    _write(clone, "target.txt", "SECRET")
    (clone / "linked").mkdir()
    os.symlink("../target.txt", clone / "linked" / "CODE_CHECK.md")
    sh(clone, "add", "-A")
    sh(clone, "commit", "-m", "guidance")
    git = Git(SubprocessRunner(), clone)
    g = from_base(git, "HEAD", ["services/api/src/x.py", "linked/y.py", ":(glob)*/z.py"])
    assert g.text == "root rules\n\n" + API_HEADER + "api rules\n"
    assert g.to_meta() == {"source": "base", "truncated": False, "files": ["CODE_CHECK.md", "services/api/CODE_CHECK.md"], "skipped": []}
    assert from_base(git, "HEAD", ["docs/x.md"]) == Guidance("base", "root rules\n", False)
    assert from_base(git, "no-such-ref", ["services/api/x.py"]) is None
    assert git.regular_files("HEAD", ["web/CODE_CHECK.md", "linked/CODE_CHECK.md", "services", "nope/CODE_CHECK.md"]) == ["web/CODE_CHECK.md"]


def test_a_cap_on_the_directories_checked_is_noted_and_marks_the_result_truncated(tmp_path):
    _write(tmp_path, "CODE_CHECK.md", "root rules\n")
    within = [f"d{i}/x.py" for i in range(MAX_GUIDANCE_DIRS)]
    assert from_worktree(tmp_path, within) == Guidance("worktree", "root rules\n", False)
    g = from_worktree(tmp_path, [*within, "one/more.py"])
    assert g.truncated is True and g.files == ("CODE_CHECK.md",)
    assert g.text.startswith("root rules\n") and "only the 500 shallowest directories" in g.text


def test_from_base_marks_a_failed_nested_lookup_instead_of_looking_complete(tmp_path, monkeypatch):
    _, clone = make_origin_and_clone(_sub(tmp_path, "lookup"))
    _write(clone, "CODE_CHECK.md", "root rules\n")
    _write(clone, "a/CODE_CHECK.md", "a rules\n")
    sh(clone, "add", "-A")
    sh(clone, "commit", "-m", "guidance")
    git = Git(SubprocessRunner(), clone)
    with pytest.raises(GitError):
        git.regular_files("no-such-ref", ["a/CODE_CHECK.md"])

    def broken(*_a, **_k):
        raise GitError("ls-tree failed")

    monkeypatch.setattr(git, "regular_files", broken)
    g = from_base(git, "HEAD", ["a/x.py"])
    assert g.truncated is True and g.files == ("CODE_CHECK.md",)
    assert g.text.startswith("root rules\n") and "nested files could not be looked up" in g.text
