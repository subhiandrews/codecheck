from dataclasses import replace
from pathlib import Path

import pytest
from clihelp import call, make_ctx
from fakes import FakeProvider
from gitutil import commit_file, make_origin_and_clone, push_branch, sh

from codecheck.config import AddressConfig
from codecheck.models import Check, Thread, ThreadComment

pytestmark = pytest.mark.usefixtures("git_env")


def thread(tid, last_author="alice", resolved=False, outdated=False, cid=100):
    return Thread(tid, "app.py", 2, resolved, outdated, (
        ThreadComment(cid, "alice", "please rename y", "t0"), ThreadComment(cid + 1, last_author, "still not renamed", "t1")))


def own_pr_setup(tmp_path, **config_over):
    """Clone checked out on `feature`, the user's own PR (author == me)."""
    origin, clone = make_origin_and_clone(tmp_path)
    sha = push_branch(tmp_path, origin, "feature", {"app.py": "x = 1\ny = 2\n"})
    sh(clone, "fetch", "origin")
    sh(clone, "checkout", "-b", "feature", "--track", "origin/feature")
    provider = FakeProvider()
    provider.me = "me"
    pr = provider.add_pr(author="me", head_ref="feature", head_sha=sha)
    ctx = make_ctx(tmp_path, clone, provider=provider, **config_over)
    return ctx, provider, pr, clone, origin


def test_status_reports_branch_dirty_and_pushability(tmp_path):
    ctx, provider, pr, clone, _origin = own_pr_setup(tmp_path)
    _code, out = call(ctx, "status", "--pr", str(pr.ref))
    assert out["on_pr_branch"] is True and out["dirty"] is False and out["behind_base"] == 0
    assert out["local_matches_remote"] is True and out["can_push"] is True
    (clone / "scratch.txt").write_text("x")
    assert call(ctx, "status", "--pr", str(pr.ref))[1]["dirty"] is True
    provider.prs[pr.ref] = replace(pr, is_cross_repo=True)
    assert call(ctx, "status", "--pr", str(pr.ref))[1]["can_push"] is False


def test_workspace_in_place_refuses_dirty_and_forks_and_creates_worktree_elsewhere(tmp_path):
    ctx, provider, pr, clone, _origin = own_pr_setup(tmp_path)
    code, out = call(ctx, "workspace", "--pr", str(pr.ref))
    assert out == {"path": str(clone), "created": False}

    (clone / "scratch.txt").write_text("x")
    code, out = call(ctx, "workspace", "--pr", str(pr.ref))
    assert code == 1 and "uncommitted" in out["error"]
    (clone / "scratch.txt").unlink()

    provider.prs[pr.ref] = replace(pr, is_cross_repo=True)
    code, out = call(ctx, "workspace", "--pr", str(pr.ref))
    assert code == 1 and "fork" in out["error"]
    provider.prs[pr.ref] = pr

    sh(clone, "checkout", "main")  # not on the PR branch -> isolated worktree
    code, out = call(ctx, "workspace", "--pr", str(pr.ref))
    wt = Path(out["path"])
    assert out["created"] is True and wt.is_dir() and wt != clone
    assert sh(wt, "rev-parse", "HEAD").strip() == pr.head_sha
    assert sh(wt, "rev-parse", "--abbrev-ref", "HEAD").strip() == "code-check/pr-7"
    code, again = call(ctx, "workspace", "--pr", str(pr.ref))  # idempotent
    assert again["path"] == out["path"]


def test_threads_returns_only_actionable_and_reviewer_list(tmp_path):
    ctx, provider, pr, *_ = own_pr_setup(tmp_path)
    provider.threads[pr.ref] = [
        thread("T1"), thread("T2", resolved=True), thread("T3", outdated=True), thread("T4", last_author="me"),
        thread("T5", last_author="ci[bot]"),
    ]
    _code, out = call(ctx, "threads", "--pr", str(pr.ref))
    assert [t["id"] for t in out["threads"]] == ["T1", "T5"]
    assert out["reviewers"] == ["alice"] and out["head_ref"] == "feature"
    assert out["threads"][0]["comments"][0]["body"] == "please rename y"
    assert {r["type"] for r in ctx.shadow.read(out["run_id"])} == {"run"}


def test_commit_requires_paths_and_push_is_gated_by_tests(tmp_path):
    ctx, _provider, pr, clone, origin = own_pr_setup(tmp_path, address=AddressConfig(test_command="false"))
    (clone / "app.py").write_text("x = 1\nrenamed = 2\n")
    code, out = call(ctx, "commit", "--message", "fix: rename y", "--paths", "app.py")
    assert code == 0 and out["sha"] == sh(clone, "rev-parse", "HEAD").strip()

    code, out = call(ctx, "push", "--pr", str(pr.ref))  # test_command = false
    assert out["pushed"] is False and out["tests_passed"] is False
    assert sh(origin, "rev-parse", "feature").strip() == pr.head_sha  # nothing was pushed

    ctx.config = replace(ctx.config, address=AddressConfig(test_command="true"))
    code, out = call(ctx, "push", "--pr", str(pr.ref))
    assert out["pushed"] is True and sh(origin, "rev-parse", "feature").strip() == out["head_sha"]


def test_push_refuses_dirty_tree_non_fast_forward_and_forks(tmp_path):
    ctx, provider, pr, clone, origin = own_pr_setup(tmp_path)
    (clone / "scratch.txt").write_text("x")
    code, out = call(ctx, "push", "--pr", str(pr.ref))
    assert code == 1 and "uncommitted" in out["error"]
    (clone / "scratch.txt").unlink()

    other = tmp_path / "other"  # someone else pushes to the PR branch
    sh(tmp_path, "clone", "-b", "feature", str(origin), str(other))
    commit_file(other, "theirs.txt", "t\n", "theirs")
    sh(other, "push", "origin", "feature")
    commit_file(clone, "mine.txt", "m\n", "mine")
    code, out = call(ctx, "push", "--pr", str(pr.ref))
    assert out["pushed"] is False and "new commits" in out["reason"]

    provider.prs[pr.ref] = replace(pr, is_cross_repo=True)
    code, out = call(ctx, "push", "--pr", str(pr.ref))
    assert code == 1 and "fork" in out["error"]


def test_reply_resolve_request_review(tmp_path):
    ctx, provider, pr, clone, _ = own_pr_setup(tmp_path)
    provider.threads[pr.ref] = [thread("T1")]
    code, out = call(ctx, "reply", "--pr", str(pr.ref), "--thread", "T1", stdin="Done in the next commit.")
    assert out["replied"] is True
    (_ref, tid, body), = provider.replies
    assert tid == "T1" and body.startswith("Done in the next commit.") and "code-check:reply" in body

    sha = sh(clone, "rev-parse", "HEAD").strip()
    code, out = call(ctx, "resolve", "--pr", str(pr.ref), "--thread", "T1", "--commit", sha)
    assert out["resolved"] is True and provider.resolved == ["T1"]
    code, out = call(ctx, "resolve", "--pr", str(pr.ref), "--thread", "T1", "--commit", "deadbeef")
    assert code == 1 and "commit" in out["error"]
    code, out = call(ctx, "resolve", "--pr", str(pr.ref), "--thread", "NOPE", "--commit", sha)
    assert code == 1 and "thread" in out["error"]

    call(ctx, "request-review", "--pr", str(pr.ref), "--users", "alice,bob")
    assert provider.requested == [(pr.ref, ["alice", "bob"])]


def test_resolve_can_be_disabled_by_config(tmp_path):
    ctx, provider, pr, clone, _ = own_pr_setup(tmp_path, address=AddressConfig(resolve_fixed_threads=False))
    provider.threads[pr.ref] = [thread("T1")]
    sha = sh(clone, "rev-parse", "HEAD").strip()
    _code, out = call(ctx, "resolve", "--pr", str(pr.ref), "--thread", "T1", "--commit", sha)
    assert out["resolved"] is False and provider.resolved == []


def test_checks_attempts_and_logs(tmp_path):
    ctx, provider, pr, *_ = own_pr_setup(tmp_path, address=AddressConfig(max_ci_attempts=2))
    provider.checks[pr.ref] = [Check("build", "fail", "", "999"), Check("lint", "pass")]
    provider.logs["build"] = "error: boom"
    _code, out = call(ctx, "checks", "--pr", str(pr.ref))
    assert out["failing"] == ["build"] and out["max_attempts"] == 2
    assert call(ctx, "check-log", "--pr", str(pr.ref), "--check", "build")[1]["log"] == "error: boom"
    assert call(ctx, "attempt", "--pr", str(pr.ref), "--check", "build")[1] == {"attempt": 1, "exhausted": False}
    assert call(ctx, "attempt", "--pr", str(pr.ref), "--check", "build")[1] == {"attempt": 2, "exhausted": False}
    assert call(ctx, "attempt", "--pr", str(pr.ref), "--check", "build")[1] == {"attempt": 3, "exhausted": True}
    provider.checks[pr.ref] = [Check("build", "pass")]  # fixed: counter resets
    call(ctx, "checks", "--pr", str(pr.ref))
    assert call(ctx, "attempt", "--pr", str(pr.ref), "--check", "build")[1]["attempt"] == 1
    assert call(ctx, "check-log", "--pr", str(pr.ref), "--check", "nope")[0] == 1


def test_list_work_review_mode_filters_and_caps(tmp_path):
    _origin, clone = make_origin_and_clone(tmp_path)
    provider = FakeProvider()
    provider.me = "me"
    refs = []
    for n in range(1, 9):
        pr = provider.add_pr(n, head_sha=f"sha{n}")
        refs.append(pr.ref)
    provider.prs[refs[0]] = replace(provider.prs[refs[0]], is_draft=True)
    provider.prs[refs[1]] = replace(provider.prs[refs[1]], author="me")
    provider.listing["review-requested"] = refs
    ctx = make_ctx(tmp_path, clone, provider=provider)

    _code, out = call(ctx, "list-work", "--mode", "review")
    assert [i["pr"] for i in out["items"]] == [str(r) for r in refs[2:7]]  # drafts + own skipped, capped at 5
    first = out["items"][0]
    call(ctx, "mark-done", "--mode", "review", "--pr", first["pr"], "--fingerprint", first["fingerprint"])
    _code, out = call(ctx, "list-work", "--mode", "review")
    assert [i["pr"] for i in out["items"]] == [str(r) for r in refs[3:8]]
    provider.prs[refs[2]] = replace(provider.prs[refs[2]], head_sha="new-sha")  # new push -> reappears
    assert str(refs[2]) in [i["pr"] for i in call(ctx, "list-work", "--mode", "review")[1]["items"]]


def test_list_work_babysit_mode_uses_threads_and_checks_and_skips_forks(tmp_path):
    _origin, clone = make_origin_and_clone(tmp_path)
    provider = FakeProvider()
    provider.me = "me"
    quiet, noisy, failing, fork = (provider.add_pr(n, author="me", head_sha=f"sha{n}") for n in (1, 2, 3, 4))
    provider.prs[fork.ref] = replace(fork, is_cross_repo=True)
    provider.threads[noisy.ref] = [thread("T1")]
    provider.threads[fork.ref] = [thread("T9")]
    provider.checks[failing.ref] = [Check("build", "fail")]
    provider.listing["authored"] = [quiet.ref, noisy.ref, failing.ref, fork.ref]
    ctx = make_ctx(tmp_path, clone, provider=provider)
    _code, out = call(ctx, "list-work", "--mode", "babysit")
    assert [i["pr"] for i in out["items"]] == [str(noisy.ref), str(failing.ref)]
    assert any("fork" in s["reason"] for s in out["skipped"])
    item = out["items"][0]
    call(ctx, "mark-done", "--mode", "babysit", "--pr", item["pr"], "--fingerprint", item["fingerprint"])
    assert [i["pr"] for i in call(ctx, "list-work", "--mode", "babysit")[1]["items"]] == [str(failing.ref)]


def test_commit_rejects_paths_escaping_the_repo(tmp_path):
    ctx, _provider, _pr, _clone, _ = own_pr_setup(tmp_path)
    (tmp_path / "outside.txt").write_text("x")
    code, out = call(ctx, "commit", "--message", "m", "--paths", "../outside.txt")
    assert code == 1 and "escapes" in out["error"]
    code, out = call(ctx, "commit", "--message", "m", "--paths", "/etc/hosts")
    assert code == 1 and "escapes" in out["error"]


def test_threads_survives_shadow_failure(tmp_path):
    ctx, provider, pr, *_ = own_pr_setup(tmp_path)
    provider.threads[pr.ref] = [thread("T1")]
    ctx.shadow.append = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    code, out = call(ctx, "threads", "--pr", str(pr.ref))
    assert code == 0 and [t["id"] for t in out["threads"]] == ["T1"]


def _to_worktree(ctx, provider, pr, clone):
    sh(clone, "checkout", "main")
    return Path(call(ctx, "workspace", "--pr", str(pr.ref))[1]["path"])


def test_workspace_rerun_preserves_uncommitted_and_unpushed_work(tmp_path):
    ctx, provider, pr, clone, _origin = own_pr_setup(tmp_path)
    wt = _to_worktree(ctx, provider, pr, clone)
    (wt / "app.py").write_text("edited = 1\n")
    code, out = call(ctx, "workspace", "--pr", str(pr.ref))
    assert code == 1 and "uncommitted" in out["error"]
    assert (wt / "app.py").read_text() == "edited = 1\n"
    sh(wt, "commit", "-am", "local")
    local = sh(wt, "rev-parse", "HEAD").strip()
    code, out = call(ctx, "workspace", "--pr", str(pr.ref))
    assert code == 1 and "local commits" in out["error"]
    assert sh(wt, "rev-parse", "HEAD").strip() == local


def test_push_refuses_closed_pr_wrong_repo_and_unexpected_branch(tmp_path):
    ctx, provider, pr, clone, origin = own_pr_setup(tmp_path)
    provider.prs[pr.ref] = replace(pr, state="merged")
    _code, out = call(ctx, "push", "--pr", str(pr.ref))
    assert out["pushed"] is False and "merged" in out["reason"]
    provider.prs[pr.ref] = pr
    other = replace(pr.ref, repo="o/elsewhere")
    provider.prs[other] = replace(pr, ref=other)
    _code, out = call(ctx, "push", "--pr", str(other))
    assert out["pushed"] is False and "not o/elsewhere" in out["reason"]
    sh(clone, "checkout", "-b", "random")
    _code, out = call(ctx, "push", "--pr", str(pr.ref))
    assert out["pushed"] is False and "not the PR branch" in out["reason"]
    assert sh(origin, "rev-parse", "feature").strip() == pr.head_sha


def test_resolve_requires_commit_on_pr_head(tmp_path):
    ctx, provider, pr, clone, _origin = own_pr_setup(tmp_path)
    provider.threads[pr.ref] = [thread("T1")]
    commit_file(clone, "fix.txt", "f\n", "fix")
    sha = sh(clone, "rev-parse", "HEAD").strip()
    _code, out = call(ctx, "resolve", "--pr", str(pr.ref), "--thread", "T1", "--commit", sha)
    assert out["resolved"] is False and "not on the PR head" in out["reason"] and provider.resolved == []
    sh(clone, "push", "origin", "HEAD:feature")
    _code, out = call(ctx, "resolve", "--pr", str(pr.ref), "--thread", "T1", "--commit", sha)
    assert out["resolved"] is True and provider.resolved == ["T1"]


def test_commit_from_subdirectory_uses_cwd_relative_paths(tmp_path):
    ctx, _provider, _pr, clone, _ = own_pr_setup(tmp_path)
    (clone / "sub").mkdir()
    (clone / "sub" / "x.txt").write_text("x")
    ctx.git = type(ctx.git)(ctx.runner, clone / "sub")
    code, _out = call(ctx, "commit", "--message", "m", "--paths", "x.txt")
    assert code == 0 and "sub/x.txt" in sh(clone, "show", "--name-only", "--format=", "HEAD")
    assert call(ctx, "commit", "--message", "m", "--paths", "../../outside")[0] == 1
