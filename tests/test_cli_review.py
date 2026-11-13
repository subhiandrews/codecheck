import json
from pathlib import Path

import pytest

from clihelp import call, make_ctx, write_findings
from codecheck.config import ReviewConfig
from fakes import FakeProvider, FakeRunner
from gitutil import make_origin_and_clone, push_branch, sh

pytestmark = pytest.mark.usefixtures("git_env")


def finish_review(ctx, out, per_member=None, aggregated=None):
    """Simulate the agents: every panel member writes a findings file, then the aggregator writes its own."""
    run_dir = Path(out["run_dir"])
    members = out["panel"]["members"]
    write_findings(run_dir, {m: (per_member or {}).get(m, []) for m in members})
    write_findings(run_dir, {"aggregator": aggregated if aggregated is not None else {"findings": [], "dropped": []}})
    return run_dir


def test_local_flow_and_unchanged_noop_and_seen_suppression(tmp_path):
    _, clone = make_origin_and_clone(tmp_path)
    ctx = make_ctx(tmp_path, clone)
    sh(clone, "checkout", "-b", "feat")

    code, out = call(ctx, "precheck", "--mode", "local")
    assert code == 0 and out["proceed"] is False and "no changes" in out["reason"]

    (clone / "app.py").write_text("x = 1\ny = 2\n")  # untracked new file counts
    code, out = call(ctx, "precheck", "--mode", "local")
    assert out["proceed"] and out["files"] == ["app.py"]

    code, out = call(ctx, "context", "--mode", "local")
    run_id = out["run_id"]
    assert out["panel"]["languages"] == {"python": ["app.py"]} and "lang-python" in out["panel"]["members"]
    meta = json.loads((Path(out["run_dir"]) / "context.json").read_text())
    assert (Path(out["run_dir"]) / meta["files"][0]["patch_file"]).read_text().startswith("diff --git a/app.py")

    finding = {"file": "app.py", "line": 2, "severity": "warning", "category": "bug", "message": "y is unused"}
    run_dir = finish_review(ctx, out, {"skeptic": [finding]}, {"findings": [{**finding, "reviewers": ["skeptic"]}], "dropped": []})
    code, ing = call(ctx, "ingest", "--run", run_id)
    assert ing["incomplete"] is False and ing["valid"]["skeptic"] == 1

    code, res = call(ctx, "post", "--run", run_id)
    assert res["new_count"] == 1 and "app.py:2" in res["text"]
    assert ctx.provider.posted == []  # review-local never posts

    code, out = call(ctx, "precheck", "--mode", "local")
    assert out["proceed"] is False and "unchanged" in out["reason"]

    code, out = call(ctx, "precheck", "--mode", "local", "--force")
    assert out["proceed"]
    code, out2 = call(ctx, "context", "--mode", "local")
    finish_review(ctx, out2, aggregated={"findings": [{**finding, "reviewers": ["skeptic"]}]})
    call(ctx, "ingest", "--run", out2["run_id"])
    code, res = call(ctx, "post", "--run", out2["run_id"])
    assert res["new_count"] == 0 and res["unchanged_count"] == 1


def pr_setup(tmp_path, **config_over):
    origin, clone = make_origin_and_clone(tmp_path)
    sha = push_branch(tmp_path, origin, "feature", {"app.py": "x = 1\ny = 2\n"})
    provider = FakeProvider()
    pr = provider.add_pr(head_ref="feature", head_sha=sha)
    provider.files[pr.ref] = ["app.py"]
    ctx = make_ctx(tmp_path, clone, provider=provider, **config_over)
    return ctx, provider, pr


def run_pr_context(ctx, pr):
    code, out = call(ctx, "precheck", "--mode", "pr", "--pr", str(pr.ref))
    assert out["proceed"], out
    code, out = call(ctx, "context", "--mode", "pr", "--pr", str(pr.ref))
    assert code == 0, out
    return out


def test_pr_flow_posts_one_inline_review_and_marks_head(tmp_path):
    ctx, provider, pr = pr_setup(tmp_path)
    out = run_pr_context(ctx, pr)
    assert Path(out["root"]).is_dir() and Path(out["root"]) != ctx.cwd  # reviewed in a separate worktree
    findings = [
        {"file": "app.py", "line": 2, "severity": "warning", "category": "bug", "message": "y unused", "reviewers": ["skeptic"]},
        {"file": "app.py", "line": 500, "severity": "suggestion", "category": "style", "message": "far away"},
    ]
    finish_review(ctx, out, per_member={"skeptic": [findings[0]]},
                  aggregated={"findings": findings, "dropped": [{"reason": "dup"}]})
    call(ctx, "ingest", "--run", out["run_id"])
    code, res = call(ctx, "post", "--run", out["run_id"])
    assert res["posted"] and res["inline"] == 1 and res["unlisted"] == 1 and res["decision"] == "COMMENT"
    (review,) = provider.posted
    assert review["head_sha"] == pr.head_sha and len(review["comments"]) == 1
    assert review["comments"][0].path == "app.py" and review["comments"][0].line == 2
    assert "code-check:finding:" in review["comments"][0].body
    assert "far away" in review["body"]  # un-anchorable finding is listed in the summary
    assert ctx.state.get(f"review:{pr.ref}") == pr.head_sha
    code, again = call(ctx, "precheck", "--mode", "pr", "--pr", str(pr.ref))
    assert again["proceed"] is False and "already reviewed" in again["reason"]
    assert "run_dir" in out and ctx.shadow.runs_for_pr(str(pr.ref)) == [out["run_id"]]
    kinds = {r["type"] for r in ctx.shadow.read(out["run_id"])}
    assert {"run", "reviewer_finding", "finding", "decision", "dropped"} <= kinds


def test_stale_head_aborts_posting(tmp_path):
    from dataclasses import replace

    ctx, provider, pr = pr_setup(tmp_path)
    out = run_pr_context(ctx, pr)
    finish_review(ctx, out)
    call(ctx, "ingest", "--run", out["run_id"])
    provider.prs[pr.ref] = replace(pr, head_sha="f" * 40)  # author pushed while we were reviewing
    code, res = call(ctx, "post", "--run", out["run_id"])
    assert code == 0 and res["posted"] is False and res["stale"] is True
    assert provider.posted == [] and ctx.state.get(f"review:{pr.ref}") is None


def test_missing_or_invalid_reviewer_makes_review_incomplete_and_never_approves(tmp_path):
    ctx, provider, pr = pr_setup(tmp_path, review=ReviewConfig(auto_approve=True))
    out = run_pr_context(ctx, pr)
    run_dir = Path(out["run_dir"])
    members = out["panel"]["members"]
    write_findings(run_dir, {m: [] for m in members[2:]})  # architect + pragmatist never wrote a file
    (run_dir / "findings" / "skeptic.json").write_text("{not json")
    write_findings(run_dir, {"aggregator": {"findings": []}})
    code, ing = call(ctx, "ingest", "--run", out["run_id"])
    assert ing["incomplete"] is True
    assert set(ing["missing"]) == {"architect", "pragmatist"} and "skeptic" in ing["invalid"]
    code, res = call(ctx, "post", "--run", out["run_id"])
    assert res["decision"] == "COMMENT" and res["incomplete"] is True
    assert "not approved" in provider.posted[0]["body"]
    assert ctx.state.get(f"review:{pr.ref}") is None  # a rerun on the same head is still allowed


def test_clean_complete_review_auto_approves_when_enabled(tmp_path):
    ctx, provider, pr = pr_setup(tmp_path, review=ReviewConfig(auto_approve=True))
    out = run_pr_context(ctx, pr)
    finish_review(ctx, out)
    call(ctx, "ingest", "--run", out["run_id"])
    code, res = call(ctx, "post", "--run", out["run_id"])
    assert res["decision"] == "APPROVE" and provider.posted[0]["decision"].event == "APPROVE"


def test_inline_comment_cap_overflows_into_the_summary(tmp_path):
    ctx, provider, pr = pr_setup(tmp_path, review=ReviewConfig(max_inline_comments=1))
    out = run_pr_context(ctx, pr)
    two = [{"file": "app.py", "line": n, "severity": "warning", "category": "bug", "message": f"issue {n}"} for n in (1, 2)]
    finish_review(ctx, out, aggregated={"findings": two})
    call(ctx, "ingest", "--run", out["run_id"])
    code, res = call(ctx, "post", "--run", out["run_id"])
    assert res["inline"] == 1 and res["unlisted"] == 1


def test_refuses_to_review_own_pr_and_out_of_repo_pr(tmp_path):
    ctx, provider, pr = pr_setup(tmp_path)
    provider.me = "alice"  # alice authored the PR
    code, out = call(ctx, "precheck", "--mode", "pr", "--pr", str(pr.ref))
    assert out["proceed"] is False and "own PR" in out["reason"]


def test_bad_run_id_and_missing_inputs_are_json_errors(tmp_path):
    _, clone = make_origin_and_clone(tmp_path)
    ctx = make_ctx(tmp_path, clone)
    code, out = call(ctx, "ingest", "--run", "../../etc")
    assert code == 1 and "invalid run id" in out["error"]
    code, out = call(ctx, "precheck", "--mode", "pr")
    assert code == 1 and "--pr" in out["error"]


def test_cleanup_removes_the_worktree(tmp_path):
    ctx, provider, pr = pr_setup(tmp_path)
    out = run_pr_context(ctx, pr)
    assert Path(out["root"]).exists()
    code, res = call(ctx, "cleanup", "--run", out["run_id"])
    assert res["removed"] is True and not Path(out["root"]).exists()


def test_sync_command_reports_freshness(tmp_path):
    _, clone = make_origin_and_clone(tmp_path)
    code, out = call(make_ctx(tmp_path, clone), "sync")
    assert out["fresh"] is True and out["base_ref"] == "origin/main" and out["warning"] is None


def test_doctor_reports_required_failures(tmp_path):
    runner = (FakeRunner().on("git", "--version", stdout="git version 2.50\n")
              .on("gh", "--version", stdout="gh version 2.60\n")
              .on("gh", "auth", "status", returncode=1, stderr="not logged in")
              .on("claude", "--version", stdout="1.0\n"))
    code, out = call(make_ctx(tmp_path, tmp_path, runner=runner), "doctor")
    by_name = {c["name"]: c for c in out["checks"]}
    assert out["ok"] is False and by_name["gh auth"]["ok"] is False and by_name["git"]["ok"] is True


def test_recorder_failure_never_changes_command_results(tmp_path, capsys):
    from codecheck.shadow.store import ShadowStore

    class Broken(ShadowStore):
        def append(self, run_id, record):
            raise OSError("disk full")

        def save_diff(self, run_id, diff):
            raise OSError("disk full")

    ctx, provider, pr = pr_setup(tmp_path)
    ctx.shadow = Broken(tmp_path / "shadow")
    out = run_pr_context(ctx, pr)
    finish_review(ctx, out)
    call(ctx, "ingest", "--run", out["run_id"])
    code, res = call(ctx, "post", "--run", out["run_id"])
    assert code == 0 and res["posted"] is True
    assert ctx.state.get(f"review:{pr.ref}") == pr.head_sha
    assert "shadow recording failed" in capsys.readouterr().err


def test_finding_paths_escaping_the_repo_are_rejected(tmp_path):
    ctx, provider, pr = pr_setup(tmp_path)
    out = run_pr_context(ctx, pr)
    bad = {"file": "../../etc/passwd", "line": 1, "severity": "warning", "category": "bug", "message": "x"}
    absolute = {**bad, "file": "/etc/passwd"}
    good = {"file": "app.py", "line": 2, "severity": "warning", "category": "bug", "message": "ok"}
    members = out["panel"]["members"]
    write_findings(Path(out["run_dir"]), {m: [] for m in members[1:]})
    write_findings(Path(out["run_dir"]), {members[0]: [bad, absolute, good]})
    code, ing = call(ctx, "ingest", "--run", out["run_id"])
    assert ing["valid"][members[0]] == 1 and ing["incomplete"] is True
    assert any("escapes the repository" in e for e in ing["invalid"][members[0]])
    write_findings(Path(out["run_dir"]), {"aggregator": {"findings": [bad]}})
    code, res = call(ctx, "post", "--run", out["run_id"])
    assert code == 1 and "escapes the repository" in res["error"]


def test_first_line_does_not_follow_symlinks_out_of_the_root(tmp_path):
    from codecheck.commands._common import first_line

    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "secret").write_text("#!/bin/secret\n")
    (root / "link").symlink_to(tmp_path / "secret")
    assert first_line(root, "link") == ""
    assert first_line(root, "../secret") == ""
