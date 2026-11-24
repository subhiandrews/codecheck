import hashlib
import json
import os
import re
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from clihelp import call, make_ctx
from gitutil import commit_file, make_origin_and_clone, sh

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

pytestmark = pytest.mark.usefixtures("git_env")  # real `git commit` needs an identity, which CI does not have

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


# --- patch mode: `--base REF --run RUN_ID` proposes against a git ref and writes a patch into the run directory ---


def _patch_of(run_dir):
    """Where the patch for a run goes: next to the runs, so that `cleanup` (which removes the run directory) keeps it."""
    return run_dir.parent.parent / "proposals" / f"{run_dir.name}.patch"


def _patch_ctx(tmp_path, base_text="1. Base rule.\n"):
    """A clone whose HEAD carries a CODE_CHECK.md, a run directory, and evidence for two disputed PRs."""
    ctx, clone = _ctx(tmp_path, learn_guidance=True)
    if base_text is not None:
        commit_file(clone, "CODE_CHECK.md", base_text, "add guidance")
    rid = new_run_id()
    run_dir = tmp_path / "state" / "runs" / rid
    run_dir.mkdir(parents=True)
    return ctx, clone, rid, run_dir


def test_evidence_with_a_base_reads_the_current_file_from_that_ref_not_the_working_tree(tmp_path):
    ctx, clone, _, _ = _patch_ctx(tmp_path)
    (clone / "CODE_CHECK.md").write_text("DIRTY local edit\n")
    ev = call(ctx, "guidance", "evidence", "--base", "HEAD")[1]
    assert ev["current"] == "1. Base rule.\n" and ev["path"] == "CODE_CHECK.md"


def test_apply_with_a_base_writes_a_patch_in_the_run_directory_and_never_touches_the_working_tree(tmp_path):
    ctx, clone, rid, run_dir = _patch_ctx(tmp_path)
    (clone / "CODE_CHECK.md").write_text("DIRTY local edit\n")
    ev = _with_evidence(ctx)
    code, out = call(ctx, "guidance", "apply", "--base", "HEAD", "--run", rid, "--digest", ev["digest"],
                     stdin="1. Base rule.\n2. Style findings are not wanted here.\n")
    assert code == 0 and out["patch"] == str(_patch_of(run_dir)) and out["created"] is False
    shutil.rmtree(run_dir)  # what `cleanup` does at the end of a review
    assert "+2. Style findings are not wanted here." in (_patch_of(run_dir)).read_text()
    assert (clone / "CODE_CHECK.md").read_text() == "DIRTY local edit\n"  # the working tree is not written
    assert sh(clone, "status", "--porcelain").strip() == "M CODE_CHECK.md"  # and nothing is staged or committed
    assert "written" not in out


def test_the_patch_applies_to_a_clean_checkout_of_the_base(tmp_path):
    ctx, clone, rid, run_dir = _patch_ctx(tmp_path)
    ev = _with_evidence(ctx)
    new = "1. Base rule.\n2. Style findings are not wanted here.\n"
    assert call(ctx, "guidance", "apply", "--base", "HEAD", "--run", rid, "--digest", ev["digest"], stdin=new)[0] == 0
    sh(clone, "apply", "--check", str(_patch_of(run_dir)))
    sh(clone, "apply", str(_patch_of(run_dir)))
    assert (clone / "CODE_CHECK.md").read_text() == new


def test_a_patch_can_create_a_file_the_base_does_not_have_and_a_directory_only_the_base_has(tmp_path):
    ctx, clone, rid, run_dir = _patch_ctx(tmp_path, base_text=None)
    commit_file(clone, "lib/mod.py", "x = 1\n", "add lib")  # `lib` exists at HEAD ...
    sh(clone, "rm", "-r", "-q", "lib")  # ... but not in the working tree
    repo = call(ctx, "guidance", "evidence")[1]["repo"]
    seed(ctx.shadow, 1, [("a1", "lib/x.py", "style", "disputed")], repo=repo)
    seed(ctx.shadow, 2, [("a2", "lib/y.py", "style", "disputed")], repo=repo)
    ev = call(ctx, "guidance", "evidence", "--dir", "lib", "--base", "HEAD")[1]
    assert ev["path"] == "lib/CODE_CHECK.md" and ev["current"] is None and ev["proposable"]
    code, out = call(ctx, "guidance", "apply", "--dir", "lib", "--base", "HEAD", "--run", rid,
                     "--digest", ev["digest"], stdin="1. Lib rule.\n")
    assert code == 0 and out["created"] is True and "+++ b/lib/CODE_CHECK.md" in (_patch_of(run_dir)).read_text()
    assert not (clone / "lib").exists()
    sh(clone, "apply", "--check", str(_patch_of(run_dir)))  # a new file needs a /dev/null header to apply


def test_a_patch_is_checked_like_a_written_proposal_and_a_refusal_leaves_no_patch(tmp_path):
    ctx, _, rid, run_dir = _patch_ctx(tmp_path)
    ev = _with_evidence(ctx)
    for text, why in ((f"key {SECRET}\n", "secret"), ("1. Base rule.\n", "does not change")):
        code, out = call(ctx, "guidance", "apply", "--base", "HEAD", "--run", rid, "--digest", ev["digest"], stdin=text)
        assert code == 1 and why in json.dumps(out)
    assert call(ctx, "guidance", "apply", "--base", "HEAD", "--run", rid, "--digest", "0" * 16, stdin="x\n")[0] == 1
    assert not (_patch_of(run_dir)).exists()
    assert call(ctx, "guidance", "evidence", "--base", "HEAD")[1]["changed"] is True  # a refusal is not remembered


def test_the_same_evidence_is_not_proposed_again_after_a_patch(tmp_path):
    ctx, _, rid, _ = _patch_ctx(tmp_path)
    ev = _with_evidence(ctx)
    assert call(ctx, "guidance", "apply", "--base", "HEAD", "--run", rid, "--digest", ev["digest"], stdin="1. New.\n")[0] == 0
    assert call(ctx, "guidance", "evidence", "--base", "HEAD")[1]["changed"] is False


def test_base_and_run_go_together_and_the_run_must_be_a_real_run_directory(tmp_path):
    ctx, _, rid, run_dir = _patch_ctx(tmp_path)
    ev = _with_evidence(ctx)
    digest = ev["digest"]
    assert call(ctx, "guidance", "apply", "--base", "HEAD", "--digest", digest, stdin="x\n")[0] == 1
    assert call(ctx, "guidance", "apply", "--run", rid, "--digest", digest, stdin="x\n")[0] == 1
    for bad in ("../escape", "/tmp", "nothex000000", new_run_id()):  # the last is well formed but has no directory
        code, _ = call(ctx, "guidance", "apply", "--base", "HEAD", "--run", bad, "--digest", digest, stdin="x\n")
        assert code == 1, bad
    assert not any(run_dir.iterdir()) and not _patch_of(run_dir).exists()


def test_a_base_that_could_act_as_an_option_or_does_not_exist_is_refused(tmp_path):
    ctx, _, rid, _ = _patch_ctx(tmp_path)
    ev = _with_evidence(ctx)
    for base in ("--output=/tmp/x", "-n", "no-such-ref"):
        assert call(ctx, "guidance", "evidence", f"--base={base}")[0] == 1, base  # `=` so argparse takes it as the value
        code, _ = call(ctx, "guidance", "apply", f"--base={base}", "--run", rid, "--digest", ev["digest"], stdin="x\n")
        assert code == 1, base


def test_a_symlink_or_directory_named_code_check_md_at_the_base_is_refused_not_treated_as_missing(tmp_path):
    ctx, clone, rid, run_dir = _patch_ctx(tmp_path, base_text=None)
    ev = _with_evidence(ctx)  # gathered first: with the symlink committed, the working-tree reader refuses too
    os.symlink("README.md", clone / "CODE_CHECK.md")
    sh(clone, "add", "CODE_CHECK.md")
    sh(clone, "commit", "-m", "symlinked guidance")
    assert call(ctx, "guidance", "evidence", "--base", "HEAD")[0] == 1
    code, _ = call(ctx, "guidance", "apply", "--base", "HEAD", "--run", rid, "--digest", ev["digest"], stdin="1. Rule.\n")
    assert code == 1 and not (_patch_of(run_dir)).exists()


def test_a_base_file_without_a_final_newline_gives_a_patch_that_applies(tmp_path):
    ctx, clone, rid, run_dir = _patch_ctx(tmp_path, base_text="1. Base rule.")  # no trailing newline at the base
    ev = _with_evidence(ctx)
    new = "1. Base rule.\n2. Style findings are not wanted here.\n"
    assert call(ctx, "guidance", "apply", "--base", "HEAD", "--run", rid, "--digest", ev["digest"], stdin=new)[0] == 0
    patch = _patch_of(run_dir)
    assert "\\ No newline at end of file" in patch.read_text()
    sh(clone, "apply", "--check", str(patch))
    sh(clone, "apply", str(patch))
    assert (clone / "CODE_CHECK.md").read_text() == new


def test_a_directory_that_is_a_symlink_at_the_base_is_refused_not_treated_as_missing(tmp_path):
    ctx, clone, rid, run_dir = _patch_ctx(tmp_path, base_text=None)
    ev = _with_evidence(ctx)
    os.symlink("app", clone / "lib")
    sh(clone, "add", "lib")
    sh(clone, "commit", "-m", "lib is a symlink")
    assert call(ctx, "guidance", "evidence", "--dir", "lib", "--base", "HEAD")[0] == 1
    code, _ = call(ctx, "guidance", "apply", "--dir", "lib", "--base", "HEAD", "--run", rid, "--digest", ev["digest"], stdin="1. Rule.\n")
    assert code == 1 and not _patch_of(run_dir).exists()


def test_review_pr_step_8_says_to_skip_when_base_warning_is_set():
    step = next(line for line in (Path(__file__).parent.parent / "skills" / "review-pr" / "SKILL.md").read_text().splitlines()
                if line.startswith("8. "))
    assert re.search(r"If `base_warning` is set.*skip this step", step)


def test_no_runs_says_no_review_pr_runs(shadow):
    assert "no review-pr runs" in gather(shadow, REPO, "")["reason"]


def test_findings_without_any_response_say_so(shadow):
    seed(shadow, 1, [("a0", "app/x.py", "style", "none")])
    assert "1 recorded PR review, but none has a finding that was" in gather(shadow, REPO, "")["reason"]
    seed(shadow, 2, [("a3", "app/x.py", "style", "none")])
    assert "2 recorded PR reviews, but" in gather(shadow, REPO, "")["reason"]


def test_one_disputed_pr_is_not_yet_a_pattern_and_two_remove_the_reason(shadow):
    seed(shadow, 1, [("a1", "app/x.py", "style", "disputed")])
    assert "no finding category was disputed in 2 or more PRs" in gather(shadow, REPO, "")["reason"]
    seed(shadow, 2, [("a2", "app/y.py", "style", "disputed")])
    assert "reason" not in gather(shadow, REPO, "")


def test_the_reason_names_a_contested_category_and_missed_comments(shadow):
    for pr in (1, 2):
        seed(shadow, pr, [(f"d{pr}", "app/x.py", "style", "disputed"), (f"c{pr}", "app/z.py", "style", "confirmed")])
    seed(shadow, 3, humans=[("app/x.py", "please check this", None)])
    reason = gather(shadow, REPO, "")["reason"]
    assert "confirmed in at least as many" in reason and "missed human comments come from 1 PR" in reason


def test_address_comments_runs_are_not_counted_as_reviews(shadow):
    shadow.append(new_run_id(), {"type": "run", "repo": REPO, "pr": f"{REPO}#9", "skill": "address-comments"})
    assert "no review-pr runs" in gather(shadow, REPO, "")["reason"]


def test_a_finding_with_only_a_reply_is_named(shadow):
    rid = seed(shadow, 1, [("a1", "app/x.py", "style", "none")])
    shadow.append(rid, {"type": "outcome", "finding_id": "a1", "thread_id": "t-r", "resolved": False, "outdated": False,
                        "replies": [{"author": "me", "body": "thanks"}], "reactions": {}})
    assert "1 with a reply but no verdict" in gather(shadow, REPO, "")["reason"]


def test_the_reason_mentions_misses_from_one_pr(shadow):
    seed(shadow, 1, humans=[("app/x.py", "please check this", None)])
    reason = gather(shadow, REPO, "")["reason"]
    assert "missed human comments come from 1 PR" in reason and "none has a finding that was confirmed or disputed" in reason and "under this path" not in reason


def test_a_scoped_reason_does_not_blame_findings_outside_the_directory(shadow):
    seed(shadow, 1, [("a1", "web/x.py", "style", "disputed")])
    assert "under this path" in gather(shadow, REPO, "svc")["reason"]


def test_the_reason_is_not_part_of_the_digest(shadow):
    seed(shadow, 1, [("a1", "app/x.py", "style", "disputed")])
    ev = gather(shadow, REPO, "")
    assert "reason" in ev
    rest = {k: v for k, v in ev.items() if k not in ("reason", "digest")}  # what the digest is computed from
    assert ev["digest"] == hashlib.sha256(json.dumps(rest, sort_keys=True).encode("utf-8")).hexdigest()[:16]
