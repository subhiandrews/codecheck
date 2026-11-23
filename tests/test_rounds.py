from dataclasses import replace

from clihelp import call, finish_review, pr_setup, run_pr_context
from gitutil import sh

from codecheck.commands._common import read_json
from codecheck.config import ReviewConfig
from codecheck.core.render import RUN_MARKER
from codecheck.core.rounds import changed_lines, count_reviews, hold_back
from codecheck.models import Finding, Review, Severity

DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,4 @@
 a
+b
+c
 d
@@ -10,1 +12,2 @@
 x
+y
"""


def f(line, sev, path="app.py"):
    return Finding(path, line, sev, "bug", "m")


def test_changed_lines_are_the_added_right_side_ranges():
    assert changed_lines(DIFF) == {"app.py": [[2, 3], [13, 13]]}


def test_count_reviews_counts_only_our_marked_reviews():
    ours = Review("bot", "c", "t", "COMMENTED", RUN_MARKER.format("r1"))
    other = Review("bob", "c", "t", "COMMENTED", RUN_MARKER.format("r2"))
    plain = Review("bot", "c", "t", "COMMENTED", "looks fine")
    assert count_reviews([ours, other, plain, ours], "bot") == 2


def test_hold_back_only_drops_suggestions_outside_the_changed_lines_from_the_quiet_round():
    rnd = {"number": 3, "changed": {"app.py": [[2, 3]]}}
    fs = [f(2, Severity.SUGGESTION), f(9, Severity.SUGGESTION), f(9, Severity.WARNING), f(9, Severity.CRITICAL),
          f(2, Severity.SUGGESTION, "other.py")]
    kept, held = hold_back(fs, rnd, 3)
    assert [(x.file, x.line, x.severity) for x in held] == [("app.py", 9, Severity.SUGGESTION), ("other.py", 2, Severity.SUGGESTION)]
    assert len(kept) == 3
    assert hold_back(fs, {**rnd, "number": 2}, 3)[1] == []  # too early
    assert hold_back(fs, rnd, 0)[1] == []  # disabled
    assert hold_back(fs, {"number": 5, "changed": None}, 3)[1] == []  # lines since the last review unknown
    assert hold_back(fs, None, 3)[1] == []  # local mode


def test_a_late_round_holds_back_suggestions_on_old_lines_end_to_end(tmp_path, git_env):
    ctx, provider, pr = pr_setup(tmp_path, review=ReviewConfig(quiet_after_round=2))
    me = provider.me
    first = run_pr_context(ctx, pr)
    assert first["panel"]  # round 1 has no round filter
    provider.reviews[pr.ref] = [Review(me, pr.head_sha, "2026-01-01T00:00:00Z", "COMMENTED", RUN_MARKER.format("old"))]
    # a second commit that adds line 3 of app.py
    work = tmp_path / "work-feature"
    (work / "app.py").write_text("x = 1\ny = 2\nz = 3\n")
    sh(work, "commit", "-am", "more")
    sh(work, "push", "origin", "feature")
    provider.prs[pr.ref] = replace(pr, head_sha=sh(work, "rev-parse", "HEAD").strip())
    code, out = call(ctx, "context", "--mode", "pr", "--pr", str(pr.ref))
    assert code == 0, out
    rnd = read_json(ctx.run_dir(out["run_id"]) / "context.json")["round"]
    assert rnd["number"] == 2 and rnd["changed"] == {"app.py": [[3, 3]]}
    sugg = [{"file": "app.py", "line": n, "severity": "suggestion", "category": "style", "message": f"s{n}"} for n in (1, 3)]
    warn = [{"file": "app.py", "line": 1, "severity": "warning", "category": "bug", "message": "w"}]
    finish_review(ctx, out, aggregated={"findings": sugg + warn})
    call(ctx, "ingest", "--run", out["run_id"])
    _code, res = call(ctx, "post", "--run", out["run_id"])
    assert res["held_back"] == 1 and res["findings"] == 2


def test_the_base_is_our_last_marked_review_not_any_review_by_our_login():
    from codecheck.core.rounds import last_review_commit

    marked = Review("bot", "aaa", "2026-01-01T00:00:00Z", "COMMENTED", RUN_MARKER.format("r1"))
    later_plain = Review("bot", "bbb", "2026-01-02T00:00:00Z", "COMMENTED", "a note")
    assert last_review_commit([later_plain, marked], "bot") == "aaa"
    assert last_review_commit([later_plain], "bot") == ""


def test_a_missing_last_commit_records_why_nothing_is_held_back(tmp_path, git_env):
    ctx, provider, pr = pr_setup(tmp_path)
    gone = "0" * 40
    provider.reviews[pr.ref] = [Review(provider.me, gone, "2026-01-01T00:00:00Z", "COMMENTED", RUN_MARKER.format("old"))]
    out = run_pr_context(ctx, pr)
    rnd = read_json(ctx.run_dir(out["run_id"]) / "context.json")["round"]
    assert rnd["number"] == 2 and rnd["changed"] is None and "not available" in rnd["note"]
