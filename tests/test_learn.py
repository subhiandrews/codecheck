import json
import os
from dataclasses import replace

import pytest
from clihelp import call, make_ctx
from gitutil import make_origin_and_clone

from codecheck.config import ReviewConfig
from codecheck.core.learn import (
    MAX_CHANGED_LINES,
    MAX_FILE_CHARS,
    check_proposal,
    gather,
)
from codecheck.runids import new_run_id
from codecheck.shadow.store import ShadowStore
from codecheck.vcs.git import Git

REPO = "acme/app"
SECRET = "ghp_" + "a" * 30


def seed(shadow, pr, findings=(), humans=(), repo=REPO):
    """One run on `pr`. findings: (id, file, category, label) with label disputed|confirmed|none; humans: (path, body, matches)."""
    rid = new_run_id()
    shadow.append(rid, {"type": "run", "repo": repo, "pr": f"{repo}#{pr}"})
    for i, (fid, file, category, label) in enumerate(findings):
        shadow.append(rid, {"type": "finding", "id": fid, "file": file, "category": category, "severity": "warning",
                            "message": f"{category} problem in {file}"})
        if label == "none":
            continue
        tid = f"t-{rid}-{i}"
        shadow.append(rid, {"type": "outcome", "finding_id": fid, "thread_id": tid, "resolved": False, "outdated": False,
                            "replies": [], "reactions": {}})
        shadow.append(rid, {"type": "verdict", "thread_id": tid, "verdict": "incorrect" if label == "disputed" else "valid"})
    for j, (path, body, matches) in enumerate(humans):
        shadow.append(rid, {"type": "human_comment", "thread_id": f"h-{rid}-{j}", "path": path, "line": 1, "author": "bob",
                            "body": body, "matches_finding": matches})
    return rid


@pytest.fixture
def shadow(tmp_path):
    return ShadowStore(tmp_path / "shadow")


def test_a_category_disputed_in_two_prs_is_evidence_and_in_one_pr_is_not(shadow):
    seed(shadow, 1, [("a1", "app/x.py", "style", "disputed")])
    assert gather(shadow, REPO, "")["proposable"] is False
    seed(shadow, 2, [("a2", "app/y.py", "style", "disputed")])
    ev = gather(shadow, REPO, "")
    assert ev["proposable"] is True
    assert [(d["category"], d["prs"], d["confirmed_prs"]) for d in ev["disputed"]] == [("style", 2, 0)]
    assert ev["disputed"][0]["examples"][0]["message"] == "style problem in app/x.py"


def test_a_category_that_was_confirmed_as_often_as_disputed_is_not_noise(shadow):
    seed(shadow, 1, [("a1", "app/x.py", "style", "disputed")])
    seed(shadow, 2, [("a2", "app/x.py", "style", "disputed")])
    seed(shadow, 3, [("a3", "app/x.py", "style", "confirmed")])
    seed(shadow, 4, [("a4", "app/x.py", "style", "confirmed")])
    assert gather(shadow, REPO, "")["disputed"] == []


def test_evidence_is_for_one_repository_and_one_directory(shadow):
    seed(shadow, 1, [("a1", "svc/x.py", "style", "disputed")])
    seed(shadow, 2, [("a2", "svc/y.py", "style", "disputed")])
    seed(shadow, 3, [("o1", "svc/x.py", "style", "disputed")], repo="other/repo")
    seed(shadow, 4, [("o2", "svc/x.py", "style", "disputed")], repo="other/repo")
    assert len(gather(shadow, REPO, "svc")["disputed"]) == 1
    assert gather(shadow, REPO, "web")["proposable"] is False
    assert gather(shadow, REPO, "sv")["proposable"] is False  # a prefix of a name is not a directory


def test_missed_human_comments_count_only_across_two_prs(shadow):
    seed(shadow, 1, humans=[("app/x.py", "please add a test", None)])
    ev = gather(shadow, REPO, "")
    assert ev["misses"] == [] and ev["proposable"] is False
    seed(shadow, 2, humans=[("app/y.py", "needs a docstring", None), ("app/y.py", "matched one", "f1")])
    ev = gather(shadow, REPO, "")
    assert {m["body"] for m in ev["misses"]} == {"please add a test", "needs a docstring"}
    assert ev["miss_prs"] == 2 and ev["proposable"] is True


def test_evidence_is_scrubbed_and_the_digest_follows_it(shadow):
    seed(shadow, 1, humans=[("app/x.py", f"use {SECRET} here", None)])
    seed(shadow, 2, humans=[("app/y.py", "second", None)])
    ev = gather(shadow, REPO, "")
    assert SECRET not in json.dumps(ev)
    assert gather(shadow, REPO, "")["digest"] == ev["digest"]
    seed(shadow, 3, humans=[("app/z.py", "third", None)])
    assert gather(shadow, REPO, "")["digest"] != ev["digest"]


def test_check_proposal_rules():
    assert check_proposal(None, "1. Check tests.\n") is None
    assert "empty" in check_proposal("old\n", "  \n")
    assert "at most" in check_proposal(None, "x" * (MAX_FILE_CHARS + 1))
    assert "secret" in check_proposal(None, f"token = {SECRET}\n")
    assert "does not change" in check_proposal("a\n", "a\n\n")
    assert "changes" in check_proposal(None, "\n".join(f"rule {i}" for i in range(MAX_CHANGED_LINES + 1)))
    assert check_proposal("\n".join(f"rule {i}" for i in range(60)), "\n".join(f"rule {i}" for i in range(61))) is None


def _ctx(tmp_path, **review):
    _, clone = make_origin_and_clone(tmp_path)
    ctx = make_ctx(tmp_path, clone, review=ReviewConfig(**review))
    (clone / "app").mkdir()
    return ctx, clone


def _with_evidence(ctx):
    repo = call(ctx, "guidance", "evidence")[1]["repo"]
    seed(ctx.shadow, 1, [("a1", "app/x.py", "style", "disputed")], repo=repo)
    seed(ctx.shadow, 2, [("a2", "app/y.py", "style", "disputed")], repo=repo)
    return call(ctx, "guidance", "evidence")[1]


def test_guidance_is_off_by_default(tmp_path):
    ctx, _ = _ctx(tmp_path)
    assert call(ctx, "guidance", "evidence") == (0, {"enabled": False})
    code, out = call(ctx, "guidance", "apply", "--digest", "x", stdin="rule\n")
    assert code == 1 and "review.learn_guidance" in json.dumps(out)


def test_evidence_then_apply_writes_the_file_and_the_same_evidence_is_not_proposed_again(tmp_path):
    ctx, clone = _ctx(tmp_path, learn_guidance=True)
    ev = _with_evidence(ctx)
    assert ev["enabled"] and ev["proposable"] and ev["changed"] and ev["current"] is None and ev["path"] == "CODE_CHECK.md"
    code, out = call(ctx, "guidance", "apply", "--digest", ev["digest"], stdin="1. Style findings are not wanted here.")
    assert code == 0 and out["written"] == "CODE_CHECK.md" and out["created"] is True
    assert (clone / "CODE_CHECK.md").read_text() == "1. Style findings are not wanted here.\n"
    assert "+1. Style findings are not wanted here." in out["diff"]
    again = call(ctx, "guidance", "evidence")[1]
    assert again["changed"] is False and again["current"] == "1. Style findings are not wanted here.\n"
    assert again["digest"] == ev["digest"]


def test_apply_refuses_stale_evidence_a_secret_and_no_evidence(tmp_path):
    ctx, clone = _ctx(tmp_path, learn_guidance=True)
    code, out = call(ctx, "guidance", "apply", "--digest", "0" * 16, stdin="rule\n")
    assert code == 1 and "no evidence" in json.dumps(out)
    ev = _with_evidence(ctx)
    code, out = call(ctx, "guidance", "apply", "--digest", "0" * 16, stdin="rule\n")
    assert code == 1 and "evidence changed" in json.dumps(out)
    code, out = call(ctx, "guidance", "apply", "--digest", ev["digest"], stdin=f"key {SECRET}\n")
    assert code == 1 and "secret" in json.dumps(out)
    assert not (clone / "CODE_CHECK.md").exists()
    assert call(ctx, "guidance", "evidence")[1]["changed"] is True  # a refused proposal is not remembered


def test_apply_writes_a_nested_file_only_for_a_real_directory_inside_the_repo(tmp_path):
    ctx, clone = _ctx(tmp_path, learn_guidance=True)
    repo = call(ctx, "guidance", "evidence")[1]["repo"]
    seed(ctx.shadow, 1, [("a1", "app/x.py", "style", "disputed")], repo=repo)
    seed(ctx.shadow, 2, [("a2", "app/y.py", "style", "disputed")], repo=repo)
    ev = call(ctx, "guidance", "evidence", "--dir", "app")[1]
    assert ev["dir"] == "app" and ev["path"] == "app/CODE_CHECK.md" and ev["proposable"]
    assert call(ctx, "guidance", "apply", "--dir", "app", "--digest", ev["digest"], stdin="1. App rule.\n")[0] == 0
    assert (clone / "app" / "CODE_CHECK.md").read_text() == "1. App rule.\n"
    for bad in ("../outside", "/etc", "a/../b", "no/such"):
        assert call(ctx, "guidance", "evidence", "--dir", bad)[0] == 1, bad


def test_the_default_directory_is_where_the_command_runs(tmp_path):
    ctx, clone = _ctx(tmp_path, learn_guidance=True)
    inner = replace(ctx, git=Git(ctx.runner, clone / "app"))
    out = call(inner, "guidance", "evidence")[1]
    assert out["dir"] == "app" and out["path"] == "app/CODE_CHECK.md"


def test_apply_never_writes_through_a_symlink(tmp_path):
    ctx, clone = _ctx(tmp_path, learn_guidance=True)
    ev = _with_evidence(ctx)
    secret = tmp_path / "secret.txt"
    secret.write_text("KEEP\n")
    os.symlink(secret, clone / "CODE_CHECK.md")
    code, _ = call(ctx, "guidance", "apply", "--digest", ev["digest"], stdin="1. Rule.\n")
    assert code == 1 and secret.read_text() == "KEEP\n" and (clone / "CODE_CHECK.md").is_symlink()
    assert call(ctx, "guidance", "evidence")[0] == 1


def test_evidence_without_shadow_recording_says_why(tmp_path):
    ctx, _ = _ctx(tmp_path, learn_guidance=True)
    out = call(replace(ctx, shadow=None), "guidance", "evidence")[1]
    assert out["enabled"] is True and out["proposable"] is False and "shadow" in out["reason"]
