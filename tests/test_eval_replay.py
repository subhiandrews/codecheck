import json
import subprocess
from pathlib import Path

import pytest
from fakes import FakeRunner
from test_eval_live import PROBE, LiveRunner, ReviewingRunner, _ctx

from codecheck.errors import CodeCheckError
from codecheck.evals import replay
from codecheck.evals.live import review_argv
from codecheck.evals.replay import (
    build_repo,
    case_from_handoff,
    human_comments,
    load_cases,
    resolve_base,
    run_case,
    run_cases,
    score,
    validate_case,
)

FOUND = {"file": "calc.py", "line": 2, "severity": "warning", "category": "bug", "message": "returns 2, not 1"}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def source(tmp_path, git_env):
    """A repository whose main moved on after the PR branched, so the base is not the tip of main."""
    src = tmp_path / "src"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    (src / "calc.py").write_text("def one():\n    return 1\n")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "base")
    base = git(src, "rev-parse", "HEAD").strip()
    git(src, "checkout", "-qb", "feature")
    (src / "calc.py").write_text("def one():\n    return 2\n")
    git(src, "commit", "-qam", "change")
    head = git(src, "rev-parse", "HEAD").strip()
    git(src, "checkout", "-q", "main")
    (src / "other.py").write_text("x = 1\n")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "main moves on")
    tip = git(src, "rev-parse", "HEAD").strip()
    diff = git(src, "diff", "--no-color", "--no-ext-diff", f"{base}...{head}")
    return {"path": src, "base": base, "head": head, "tip": tip, "diff": diff}


def make_case(source, **over):
    return {"name": "pr1", "repo": "o/r", "clone": str(source["path"]), "head": source["head"], "base": source["base"],
            "diff": source["diff"],
            "comments": [{"thread_id": "T1", "path": "calc.py", "line": 2, "author": "alice", "in_review": True}], **over}


def workdir(tmp_path, name="work"):
    path = tmp_path / name
    path.mkdir()
    return path


# --- the rebuilt repository --------------------------------------------------------------------------------------


def test_build_repo_puts_origin_main_at_the_base_and_head_checked_out(tmp_path, source):
    ctx = _ctx(tmp_path, LiveRunner(FakeRunner()))
    work = workdir(tmp_path)
    repo = build_repo(ctx, make_case(source), work)
    assert git(repo, "rev-parse", "origin/main").strip() == source["base"]
    assert git(repo, "rev-parse", "HEAD").strip() == source["head"]
    assert git(repo, "merge-base", "origin/main", "HEAD").strip() == source["base"]
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "remote", "get-url", "origin").strip() == str(work / "origin.git")
    # The PR's changes are committed, so the review-local diff against the base is exactly the saved diff.
    assert git(repo, "diff", "--no-color", "--no-ext-diff", source["base"]) == source["diff"]
    # The user's own clone is untouched.
    assert git(source["path"], "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert git(source["path"], "status", "--porcelain") == ""


def test_a_saved_diff_that_is_not_the_diff_of_the_commits_is_refused(tmp_path, source):
    ctx = _ctx(tmp_path, LiveRunner(FakeRunner()))
    with pytest.raises(CodeCheckError, match="not the saved diff"):
        build_repo(ctx, make_case(source, diff=source["diff"].replace("return 2", "return 3")), workdir(tmp_path))


def test_commits_missing_or_not_in_the_right_order_are_refused(tmp_path, source):
    ctx = _ctx(tmp_path, LiveRunner(FakeRunner()))
    with pytest.raises(CodeCheckError, match="is not in"):
        build_repo(ctx, make_case(source, base="0" * 40), workdir(tmp_path, "w1"))
    with pytest.raises(CodeCheckError, match="not an ancestor"):
        build_repo(ctx, make_case(source, base=source["tip"]), workdir(tmp_path, "w2"))


@pytest.mark.parametrize("over", [{"head": "abc"}, {"base": "A" * 40}, {"clone": "-evil"}, {"clone": "relative/path"},
                                  {"clone": "/no/such/dir"}, {"comments": "nope"}])
def test_invalid_cases_are_refused(source, over):
    with pytest.raises(CodeCheckError):
        validate_case(make_case(source, **over))


def test_a_case_missing_fields_is_refused_by_name(source):
    case = make_case(source)
    del case["diff"]
    with pytest.raises(CodeCheckError, match="pr1: missing diff"):
        validate_case(case)


def test_load_cases_reads_a_list_and_rejects_duplicates(tmp_path, source):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([make_case(source)]))
    assert [c["name"] for c in load_cases(path)] == ["pr1"]
    path.write_text(json.dumps([make_case(source), make_case(source)]))
    with pytest.raises(CodeCheckError, match="duplicate"):
        load_cases(path)
    path.write_text("{not json")
    with pytest.raises(CodeCheckError, match="not readable JSON"):
        load_cases(path)
    path.write_text("{}")
    with pytest.raises(CodeCheckError, match="expected a list"):
        load_cases(path)


def test_resolve_base_finds_the_merge_base_not_the_tip_of_main(tmp_path, source):
    ctx = _ctx(tmp_path, LiveRunner(FakeRunner()))
    assert resolve_base(ctx, source["path"], source["head"], source["diff"]) == source["base"]
    with pytest.raises(CodeCheckError, match="reproduces the saved diff"):
        resolve_base(ctx, source["path"], source["head"], "diff --git a/z b/z\n")


# --- scoring -----------------------------------------------------------------------------------------------------


def comment(line, author="alice", path="calc.py", **over):
    return {"thread_id": f"T{line}", "path": path, "line": line, "author": author, **over}


def test_score_matches_the_same_file_within_the_tolerance():
    findings = [{"file": "calc.py", "line": 5}, {"file": "calc.py", "line": 40}, {"file": "other.py", "line": 10}]
    out = score(findings, [comment(2), comment(10), comment(40), comment(10, path="other.py")])
    assert [r["matched"] for r in out["per_comment"]] == [True, False, True, True]
    assert (out["comments"], out["matched"], out["recall"]) == (4, 3, 0.75)
    assert (out["findings"], out["findings_on_a_comment"], out["findings_elsewhere"]) == (3, 3, 0)
    assert score(findings, [comment(2)], tolerance=2)["matched"] == 0  # line 5 is 3 away


def test_score_leaves_out_comments_it_cannot_judge_and_caps_one_authors_comments():
    findings = [{"file": "calc.py", "line": 1}]
    comments = [comment(1, in_review=False), {"thread_id": "N", "path": "calc.py", "line": None},
                comment(1, author="bob"), comment(50, author="bob"), comment(60, author="bob")]
    out = score(findings, comments)
    assert out["comments"] == 3  # the out-of-review and the line-less comment are not counted
    capped = score(findings, comments, max_per_author=1)
    assert capped["comments"] == 1 and capped["matched"] == 1


def test_score_with_no_comments_has_no_recall_and_counts_findings_as_elsewhere():
    out = score([{"file": "a.py", "line": 1}], [])
    assert out["recall"] is None and out["findings_elsewhere"] == 1


# --- running a case ----------------------------------------------------------------------------------------------


def reviewing_ctx(tmp_path, findings, cost=0.01):
    ok = json.dumps({"type": "result", "is_error": False, "result": "ok", "total_cost_usd": cost})
    fake = FakeRunner().on(*PROBE, stdout=ok).on_prefix("env", stdout=ok)
    return _ctx(tmp_path, ReviewingRunner(fake, findings)), fake


def test_run_case_reviews_the_rebuilt_repo_and_scores_the_findings(tmp_path, source):
    ctx, fake = reviewing_ctx(tmp_path, [FOUND])
    work = workdir(tmp_path)
    out = run_case(ctx, make_case(source), work)
    [call] = fake.calls
    assert Path(call.cwd) == work.resolve() / "repo" and call.input == ""
    argv = call.argv
    isolation = argv[: argv.index("claude")]
    assert f"CODE_CHECK_STATE_DIR={work.resolve()}/state" in isolation
    assert "CODE_CHECK_REVIEW__ROUTING__MODE=off" not in isolation  # the shipped panel, routing included
    assert out["got"] == [{"file": "calc.py", "line": 2, "category": "bug", "severity": "warning"}]
    assert out["score"]["recall"] == 1.0 and out["claude"]["cost_usd"] == 0.01 and "error" not in out


def test_the_fixed_panel_option_pins_the_review_like_the_live_eval(tmp_path):
    ctx = _ctx(tmp_path, FakeRunner())
    pinned = review_argv(ctx, tmp_path)
    shipped = review_argv(ctx, tmp_path, fixed_panel=False)
    assert "CODE_CHECK_REVIEW__ROUTING__MODE=off" in pinned and "CODE_CHECK_REVIEW__FULL=0" in pinned
    assert not any("REVIEW__" in a for a in shipped)
    assert "CODE_CHECK_REMOTE=origin" in shipped and "CODE_CHECK_BASE_BRANCH=main" in shipped


def test_a_failed_claude_run_is_reported_not_scored_as_clean(tmp_path, source):
    bad = json.dumps({"is_error": True, "result": "boom"})
    fake = FakeRunner().on_prefix("env", stdout=bad, returncode=1)
    out = run_case(_ctx(tmp_path, ReviewingRunner(fake, [])), make_case(source), workdir(tmp_path))
    assert out["error"].startswith("claude run failed") and out["claude"]["ok"] is False


def test_dry_run_rebuilds_and_checks_without_calling_a_model(tmp_path, source):
    fake = FakeRunner()  # any scripted-command call would raise: not even the probe may run
    ctx = _ctx(tmp_path, LiveRunner(fake))
    out = run_cases(ctx, [make_case(source)], dry_run=True)
    assert out["cases"][0]["built"] is True and out["cost_usd"] == 0 and fake.calls == []
    bad = run_cases(ctx, [make_case(source, diff="diff --git a/z b/z\n")], dry_run=True)
    assert "not the saved diff" in bad["cases"][0]["error"]  # one bad case does not stop the run


def test_the_budget_stops_new_cases_and_limit_and_only_select(tmp_path, source):
    ctx, _ = reviewing_ctx(tmp_path, [FOUND], cost=0.6)
    cases = [make_case(source, name="a"), make_case(source, name="b"), make_case(source, name="c")]
    out = run_cases(ctx, cases, budget_usd=0.5)
    assert [c["name"] for c in out["cases"]] == ["a"] and out["skipped_for_budget"] == ["b", "c"]
    assert out["cost_usd"] == 0.6
    ctx, _ = reviewing_ctx(tmp_path, [FOUND])
    assert [c["name"] for c in run_cases(ctx, cases, limit=2)["cases"]] == ["a", "b"]
    ctx, _ = reviewing_ctx(tmp_path, [FOUND])
    assert [c["name"] for c in run_cases(ctx, cases, only="c")["cases"]] == ["c"]
    with pytest.raises(CodeCheckError, match="unknown case"):
        run_cases(ctx, cases, only="zzz")


def test_an_unreachable_model_stops_before_any_case(tmp_path, source):
    ctx = _ctx(tmp_path, LiveRunner(FakeRunner().on(*PROBE, returncode=127, stderr="claude: not found")))
    with pytest.raises(CodeCheckError, match="model not reachable"):
        run_cases(ctx, [make_case(source)])


def test_the_probe_runs_once_before_the_first_review(tmp_path, source):
    ctx, fake = reviewing_ctx(tmp_path, [FOUND])
    run_cases(ctx, [make_case(source)])
    assert fake.argvs()[0] == PROBE and fake.argvs()[1][0] == "env" and len(fake.calls) == 2


# --- cases from hand-offs ----------------------------------------------------------------------------------------


def test_human_comments_leave_out_bots_and_the_owner():
    handoff = {"comparison": {"human_comments": [
        {"thread_id": "1", "path": "a.py", "line": 1, "author": "alice", "body": "secret text", "in_review": True},
        {"thread_id": "2", "path": "a.py", "line": 2, "author": "GitLabDuo"},
        {"thread_id": "3", "path": "a.py", "line": 3, "author": "dependabot[bot]"},
        {"thread_id": "4", "path": "a.py", "line": 4, "author": "Me"}]}}
    assert human_comments(handoff, owner="me") == [
        {"thread_id": "1", "path": "a.py", "line": 1, "author": "alice", "in_review": True}]  # no comment text kept


def test_case_from_handoff_resolves_the_base_and_keeps_only_human_positions(tmp_path, source):
    shadow = tmp_path / "shadow"
    (shadow / "abcdef123456.handoff").mkdir(parents=True)
    handoff = {"repo": "o/r", "pr": 7, "head_sha": source["head"], "tool_version": "1.1.0",
               "comparison": {"human_comments": [{"thread_id": "T1", "path": "calc.py", "line": 2, "author": "alice"}]}}
    (shadow / "abcdef123456.handoff" / "handoff.json").write_text(json.dumps(handoff))
    (shadow / "abcdef123456.diff").write_text(source["diff"])
    ctx = _ctx(tmp_path, LiveRunner(FakeRunner()))
    case = case_from_handoff(ctx, shadow, "abcdef123456", source["path"])
    assert case["base"] == source["base"] and case["head"] == source["head"] and case["name"] == "o/r#7@abcdef123456"
    assert case["comments"][0]["author"] == "alice" and case["recorded_tool_version"] == "1.1.0"
    validate_case(case)
    (shadow / "abcdef123456.handoff" / "handoff.json").write_text(json.dumps({**handoff, "pr": "o/r#7"}))
    assert case_from_handoff(ctx, shadow, "abcdef123456", source["path"])["name"] == "o/r#7@abcdef123456"
    for bad in ("../x", "ABCDEF123456", "abc"):
        with pytest.raises(CodeCheckError, match="12-character"):
            case_from_handoff(ctx, shadow, bad, source["path"])
    with pytest.raises(CodeCheckError, match="no readable hand-off"):
        case_from_handoff(ctx, shadow, "000000000000", source["path"])


def test_the_command_line_runs_a_dry_run_and_reports_errors_as_json(tmp_path, source, monkeypatch, capsys):
    ctx = _ctx(tmp_path, LiveRunner(FakeRunner()))
    monkeypatch.setattr(replay, "_context", lambda: ctx)
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([make_case(source)]))
    out_file = tmp_path / "results.jsonl"
    assert replay.main(["run", str(cases), "--dry-run", "--out", str(out_file)]) == 0
    assert json.loads(capsys.readouterr().out)["cases"][0]["built"] is True
    assert json.loads(out_file.read_text().splitlines()[0])["name"] == "pr1"
    assert replay.main(["run", str(tmp_path / "missing.json")]) == 1
    assert "error" in json.loads(capsys.readouterr().out)
