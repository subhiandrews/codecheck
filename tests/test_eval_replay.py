import json
import subprocess
import sys
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
    seed_cases,
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
                                  {"clone": "/no/such/dir"}, {"comments": "nope"}, {"comments": ["a string"]},
                                  {"diff": None}, {"repo": 5}, {"name": 3}])
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
    assert out["got"] == [{"file": "calc.py", "line": 2, "category": "bug", "severity": "warning", "message": FOUND["message"]}]
    assert out["score"]["recall"] == 1.0 and out["claude"]["cost_usd"] == 0.01 and "error" not in out


def test_a_case_seed_is_written_and_handed_to_the_review(tmp_path, source):
    seed = "## calc.py:2\n- code-check [ours]:\n  > **Warning** returns 2, not 1\n"
    ctx, fake = reviewing_ctx(tmp_path, [FOUND])
    work = workdir(tmp_path)
    run_case(ctx, make_case(source, seed_comments=seed), work)
    [call] = fake.calls
    path = work.resolve() / "seed-comments.md"
    assert f"CODE_CHECK_REVIEW__SEED_COMMENTS={path}" in call.argv[: call.argv.index("claude")]
    assert path.read_text() == seed
    ctx2, fake2 = reviewing_ctx(tmp_path, [FOUND])
    run_case(ctx2, make_case(source), workdir(tmp_path, "unseeded"))
    argv2 = fake2.calls[0].argv
    # an unseeded case clears the key, so a seed in the maintainer's own config cannot skew the baseline
    assert "CODE_CHECK_REVIEW__SEED_COMMENTS=" in argv2[: argv2.index("claude")]


def test_a_replay_runs_the_cli_of_this_checkout_not_a_cached_venv(tmp_path, source):
    # bin/code-check bootstraps a venv that is only rebuilt when VERSION changes, so a replay would otherwise measure
    # (and silently drop a seed for) an older build
    ctx, fake = reviewing_ctx(tmp_path, [FOUND])
    run_case(ctx, make_case(source), workdir(tmp_path))
    assert f"CODE_CHECK_PYTHON={sys.executable}" in fake.calls[0].argv[: fake.calls[0].argv.index("claude")]
    ctx.env = {"CODE_CHECK_PYTHON": "/my/python"}  # the user's own choice is not overridden
    assert not any(a.startswith("CODE_CHECK_PYTHON=") for a in review_argv(ctx, tmp_path, pin_cli=True))
    assert not any(a.startswith("CODE_CHECK_PYTHON=") for a in review_argv(_ctx(tmp_path, FakeRunner()), tmp_path))  # live eval unchanged


def test_a_seed_must_be_text(source):
    with pytest.raises(CodeCheckError, match="seed_comments"):
        validate_case(make_case(source, seed_comments=5))


def test_seed_cases_give_each_round_the_findings_of_the_rounds_before_it(source):
    first, second, third = (make_case(source, name=n) for n in ("pr-1", "pr-2", "pr-3"))
    f1 = {**FOUND, "message": "returns 2, not 1"}
    f2 = {**FOUND, "line": 9, "severity": "critical", "category": "security", "message": "another bypass"}
    results = [{"name": "pr-1", "got": [f1]}, {"name": "pr-2", "got": [f1, f2]}]
    seeded = seed_cases([first, second, third], results)
    assert "seed_comments" not in seeded[0]
    assert "returns 2, not 1" in seeded[1]["seed_comments"] and "[ours]" in seeded[1]["seed_comments"]
    assert "another bypass" not in seeded[1]["seed_comments"]
    assert seeded[2]["seed_comments"].count("returns 2, not 1") == 1  # the same finding twice is one comment
    assert "another bypass" in seeded[2]["seed_comments"] and "## calc.py:9" in seeded[2]["seed_comments"]
    assert "seed_comments" not in first  # the input cases are not changed


def test_the_fixed_panel_option_pins_the_review_like_the_live_eval(tmp_path):
    ctx = _ctx(tmp_path, FakeRunner())
    pinned = review_argv(ctx, tmp_path)
    shipped = review_argv(ctx, tmp_path, fixed_panel=False)
    assert "CODE_CHECK_REVIEW__ROUTING__MODE=off" in pinned and "CODE_CHECK_REVIEW__FULL=0" in pinned
    assert not any("REVIEW__" in a and "SEED_COMMENTS" not in a for a in shipped)  # no panel override
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


def test_the_seed_command_prints_cases_and_reports_a_bad_results_file_as_json(tmp_path, source, monkeypatch, capsys):
    monkeypatch.setattr(replay, "_context", lambda: _ctx(tmp_path, LiveRunner(FakeRunner())))
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([make_case(source, name="a"), make_case(source, name="b")]))
    results = tmp_path / "results.jsonl"
    results.write_text(json.dumps({"name": "a", "got": [FOUND]}) + "\n")
    assert replay.main(["seed", str(cases), str(results)]) == 0
    seeded = json.loads(capsys.readouterr().out)
    assert "seed_comments" not in seeded[0] and "returns 2, not 1" in seeded[1]["seed_comments"]
    results.write_bytes(b"\xff\xfe not utf-8\n")  # a results file that is not text is an error, not a traceback
    assert replay.main(["seed", str(cases), str(results)]) == 1
    assert "error" in json.loads(capsys.readouterr().out)


# --- a PR reviewed several times ---------------------------------------------------------------------------------


def test_score_credits_every_site_a_finding_names_and_groups_recall_by_round():
    findings = [{"file": "release.yml", "line": 10, "message": "Unpinned: also ci.yml:20 and docker/Dockerfile:1."}]
    comments = [comment(10, path="release.yml", round=1), comment(21, path="ci.yml", round=2),
                comment(1, path="docker/Dockerfile", round=2), comment(40, path="ci.yml", round=3)]
    out = score(findings, comments)
    assert [r["matched"] for r in out["per_comment"]] == [True, True, True, False]
    assert out["by_round"] == {1: {"comments": 1, "matched": 1}, 2: {"comments": 2, "matched": 2},
                               3: {"comments": 1, "matched": 0}}
    assert out["findings_on_a_comment"] == 1  # one finding covered three comments


def test_score_without_rounds_has_no_by_round_entries():
    assert score([{"file": "a.py", "line": 1}], [comment(1, path="a.py")])["by_round"] == {}


@pytest.fixture
def reviewed_twice(tmp_path, git_env):
    """A PR whose first head has two problems; the second head fixes one and moves the other down."""
    src = tmp_path / "multi"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    (src / "a.txt").write_text("keep\n")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "base")
    base = git(src, "rev-parse", "HEAD").strip()
    git(src, "checkout", "-qb", "feature")
    (src / "ci.yml").write_text("uses: x@v1\nuses: y@v1\n")
    (src / "docs.md").write_text("intro\nrepeats the commands\n")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "first")
    first = git(src, "rev-parse", "HEAD").strip()
    (src / "ci.yml").write_text("uses: x@sha\nuses: y@sha\n")
    git(src, "commit", "-qam", "pin")
    fix = git(src, "rev-parse", "HEAD").strip()
    (src / "docs.md").write_text("new line\nintro\nrepeats the commands\n")
    git(src, "commit", "-qam", "docs")
    second = git(src, "rev-parse", "HEAD").strip()
    labels = [
        {"id": "pins", "round": 1, "path": "ci.yml", "needle": "uses: y", "fixed_in": fix[:7]},
        {"id": "dup", "round": 2, "path": "docs.md", "needle": "repeats the commands"},
    ]
    return {"path": src, "base": base, "first": first, "second": second, "labels": labels}


def test_cases_from_git_map_each_label_to_its_line_at_each_head(tmp_path, reviewed_twice):
    r = reviewed_twice
    ctx = _ctx(tmp_path, LiveRunner(FakeRunner()))
    first, second = replay.cases_from_git(ctx, r["path"], "o/r", r["base"], r["labels"], [r["first"], r["second"]], name="multi")
    assert first["name"] == f"multi@{r['first'][:7]}" and first["head"] == r["first"]
    assert [(c["thread_id"], c["line"], c["round"]) for c in first["comments"]] == [("pins", 2, 1), ("dup", 2, 2)]
    assert [(c["thread_id"], c["line"]) for c in second["comments"]] == [("dup", 3)]  # fixed label gone, line moved
    validate_case(first)


def test_cases_from_git_refuse_a_label_the_head_no_longer_holds(tmp_path, reviewed_twice):
    r = reviewed_twice
    gone = [{"id": "x", "round": 1, "path": "docs.md", "needle": "never there"}]
    with pytest.raises(CodeCheckError, match="never there"):
        replay.cases_from_git(_ctx(tmp_path, LiveRunner(FakeRunner())), r["path"], "o/r", r["base"], gone, [r["first"]])
    twice = [{"id": "y", "round": 1, "path": "ci.yml", "needle": "uses:", "occurrence": 3}]
    with pytest.raises(CodeCheckError, match="occurrence 3"):
        replay.cases_from_git(_ctx(tmp_path, LiveRunner(FakeRunner())), r["path"], "o/r", r["base"], twice, [r["first"]])
    assert replay.cases_from_git(_ctx(tmp_path, LiveRunner(FakeRunner())), r["path"], "o/r", r["base"],
                                 [{**twice[0], "occurrence": 2}], [r["first"]])[0]["comments"][0]["line"] == 2


def test_from_git_cases_pass_the_rebuild_check(tmp_path, reviewed_twice):
    r = reviewed_twice
    ctx = _ctx(tmp_path, LiveRunner(FakeRunner()))
    cases = replay.cases_from_git(ctx, r["path"], "o/r", r["base"], r["labels"], [r["first"], r["second"]])
    for case in cases:
        assert run_case(ctx, case, workdir(tmp_path, case["head"][:7]), dry_run=True)["built"] is True


def test_the_hermetic_pr_labels_are_well_formed():
    labels = json.loads((Path(__file__).parent.parent / "evals/replay-labels/hermetic-pr2.json").read_text())
    assert len({x["id"] for x in labels}) == len(labels) == 17
    assert {x["round"] for x in labels} == {1, 2, 3, 4, 5}
    assert all({"id", "round", "path", "needle"} <= set(x) and x["needle"] for x in labels)


def test_a_replay_keeps_each_finding_message_for_scoring(tmp_path, source):
    ctx, _ = reviewing_ctx(tmp_path, [FOUND])
    out = run_case(ctx, make_case(source), workdir(tmp_path))
    assert out["got"][0]["message"] == "returns 2, not 1"


def test_funnel_says_where_each_label_was_lost():
    raw = {"reviewers": {"skeptic": [{"file": "a.yml", "line": 5, "message": "m"}, {"file": "b.md", "line": 9}],
                         "doc-skeptic": [{"file": "c.md", "line": 3}], "pragmatist": []},
           "aggregator": {"findings": [{"file": "a.yml", "line": 5, "message": "m"}],
                          "dropped": [{"reviewers": ["skeptic"], "file": "b.md", "line": 9, "reason": "low-value"}]}}
    comments = [comment(5, path="a.yml", round=1), comment(9, path="b.md", round=2), comment(3, path="c.md", round=2),
                comment(70, path="d.py", round=3)]
    out = replay.funnel(raw, comments)
    assert [r["stage"] for r in out["per_comment"]] == ["kept", "dropped:low-value", "lost-in-aggregation", "not-reported"]
    assert out["per_comment"][2]["reported_by"] == ["doc-skeptic"]
    assert out["stages"] == {"dropped:low-value": 1, "kept": 1, "lost-in-aggregation": 1, "not-reported": 1}
    assert out["reviewer_findings"] == {"doc-skeptic": 1, "pragmatist": 0, "skeptic": 2}
    assert (out["kept"], out["dropped"]) == (1, 1) and out["dropped_findings"][0]["reason"] == "low-value"
    assert replay.funnel(None, comments) == {"error": "no raw output"}


def test_run_case_saves_the_raw_output_and_reports_the_funnel(tmp_path, source):
    ctx, _ = reviewing_ctx(tmp_path, [FOUND])
    raw_dir = tmp_path / "raw"
    out = run_case(ctx, make_case(source), workdir(tmp_path), raw_dir=raw_dir)
    assert out["funnel"]["stages"] == {"kept": 1}
    [saved] = raw_dir.glob("*.raw.json")
    assert saved.name == "pr1.raw.json" and "aggregator" in json.loads(saved.read_text())


def test_a_label_introduced_by_a_later_commit_is_left_out_of_earlier_heads(tmp_path, reviewed_twice):
    r = reviewed_twice
    ctx = _ctx(tmp_path, LiveRunner(FakeRunner()))
    labels = [{"id": "late", "round": 2, "path": "docs.md", "needle": "new line", "introduced_in": r["second"]}]
    first, second = replay.cases_from_git(ctx, r["path"], "o/r", r["base"], labels, [r["first"], r["second"]])
    assert first["comments"] == [] and [c["thread_id"] for c in second["comments"]] == ["late"]


def test_the_hermetic_pr6_labels_are_well_formed():
    labels = json.loads((Path(__file__).parent.parent / "evals/replay-labels/hermetic-pr6.json").read_text())
    assert len({x["id"] for x in labels}) == len(labels) == 26
    assert {x["round"] for x in labels} == {1, 2, 3, 4, 5, 6, 7}
    assert all({"id", "round", "path", "needle"} <= set(x) and x["needle"] for x in labels)


def test_score_reports_recall_by_kind_of_label():
    findings = [{"file": "a.py", "line": 1}]
    comments = [comment(1, path="a.py", kind="first-head"), comment(50, path="a.py", kind="fix"),
                comment(90, path="a.py", kind="tail")]
    assert score(findings, comments)["by_kind"] == {"fix": {"comments": 1, "matched": 0},
                                                    "first-head": {"comments": 1, "matched": 1},
                                                    "tail": {"comments": 1, "matched": 0}}


def test_cases_from_git_give_each_label_a_kind(tmp_path, reviewed_twice):
    r = reviewed_twice
    labels = [{"id": "a", "round": 1, "path": "ci.yml", "needle": "uses: x"},
              {"id": "b", "round": 2, "path": "docs.md", "needle": "repeats", "tail": True},
              {"id": "c", "round": 2, "path": "docs.md", "needle": "new line", "introduced_in": r["second"]}]
    cases = replay.cases_from_git(_ctx(tmp_path, LiveRunner(FakeRunner())), r["path"], "o/r", r["base"], labels, [r["second"]])
    assert {c["thread_id"]: c["kind"] for c in cases[0]["comments"]} == {"a": "first-head", "b": "tail", "c": "fix"}


def test_a_person_whose_name_contains_bot_or_duo_is_not_taken_for_a_bot():
    people = ["abbott", "Talbot", "duong", "robot-fan"]
    bots = ["github-actions", "GitHub-Actions", "dependabot[bot]", "GitLabDuo", "gitlab-duo", "copilot", "coderabbitai"]
    entries = [{"thread_id": a, "path": "a.py", "line": 1, "author": a} for a in people + bots]
    kept = [c["author"] for c in human_comments({"comparison": {"human_comments": entries}})]
    assert kept == people


def test_human_comments_tolerate_a_mistyped_hand_off():
    assert human_comments({"comparison": ["x"]}) == [] and human_comments({"comparison": {"human_comments": "x"}}) == []
    odd = {"comparison": {"human_comments": ["text", {"thread_id": "1", "author": None, "path": "a.py", "line": 1}]}}
    assert [c["thread_id"] for c in human_comments(odd)] == ["1"]


def write_handoff(tmp_path, handoff, run_id="abcdef123456", diff=""):
    shadow = tmp_path / "shadow"
    (shadow / f"{run_id}.handoff").mkdir(parents=True, exist_ok=True)
    path = shadow / f"{run_id}.handoff" / "handoff.json"
    if isinstance(handoff, bytes):
        path.write_bytes(handoff)
    else:
        path.write_text(json.dumps(handoff))
    (shadow / f"{run_id}.diff").write_text(diff)
    return shadow


def test_a_relative_clone_path_gives_an_absolute_case_clone(tmp_path, source, monkeypatch):
    shadow = write_handoff(tmp_path, {"repo": "o/r", "pr": 7, "head_sha": source["head"]}, diff=source["diff"])
    ctx = _ctx(tmp_path, LiveRunner(FakeRunner()))
    monkeypatch.chdir(source["path"].parent)
    case = case_from_handoff(ctx, shadow, "abcdef123456", Path(source["path"].name))
    assert case["clone"] == str(source["path"].resolve())
    validate_case(case)  # the clone must be an absolute path


def test_the_manifest_reads_each_hand_off_once_and_reports_bad_ones_as_json(tmp_path, source, monkeypatch, capsys):
    ctx = _ctx(tmp_path, LiveRunner(FakeRunner()))
    monkeypatch.setattr(replay, "_context", lambda: ctx)
    clone = f"o/r={source['path']}"
    good = {"repo": "o/r", "pr": 7, "head_sha": source["head"], "comparison": {"human_comments": []}}
    shadow = write_handoff(tmp_path, good, diff=source["diff"])
    assert replay.main(["manifest", "--shadow", str(shadow), "--clone", clone, "abcdef123456"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["repo"] == "o/r"
    for bad, text in ((["not", "an", "object"], "not a JSON object"), (b"\xff\xfe", "no readable hand-off"),
                      ({**good, "repo": None}, "no repository"), ({**good, "repo": "x/y"}, "no --clone for x/y"),
                      ({"repo": "o/r", "head_sha": 5}, "no head commit")):
        write_handoff(tmp_path, bad, diff=source["diff"])
        assert replay.main(["manifest", "--shadow", str(shadow), "--clone", clone, "abcdef123456"]) == 1
        assert text in json.loads(capsys.readouterr().out)["error"]
    assert replay.main(["manifest", "--shadow", str(shadow), "--clone", "nopath", "abcdef123456"]) == 1
    assert "expected OWNER/REPO=PATH" in json.loads(capsys.readouterr().out)["error"]


def test_each_result_is_handed_over_as_soon_as_it_exists(tmp_path, source, monkeypatch):
    ctx, _ = reviewing_ctx(tmp_path, [FOUND])
    seen = []
    real = replay.run_case

    def flaky(ctx, case, workdir, **kw):
        if case["name"] == "b":
            raise RuntimeError("the runner died")  # not a CodeCheckError: nothing catches it
        return real(ctx, case, workdir, **kw)

    monkeypatch.setattr(replay, "run_case", flaky)
    cases = [make_case(source, name="a"), make_case(source, name="b")]
    with pytest.raises(RuntimeError):
        run_cases(ctx, cases, on_result=seen.append)
    assert [r["name"] for r in seen] == ["a"]  # the finished, paid review was not lost


def test_the_out_file_keeps_finished_cases_when_a_later_one_dies(tmp_path, source, monkeypatch):
    ctx, _ = reviewing_ctx(tmp_path, [FOUND])
    monkeypatch.setattr(replay, "_context", lambda: ctx)
    real = replay.run_case

    def flaky(ctx, case, workdir, **kw):
        if case["name"] == "b":
            raise KeyboardInterrupt
        return real(ctx, case, workdir, **kw)

    monkeypatch.setattr(replay, "run_case", flaky)
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([make_case(source, name="a"), make_case(source, name="b")]))
    out_file = tmp_path / "results.jsonl"
    with pytest.raises(KeyboardInterrupt):
        replay.main(["run", str(cases), "--out", str(out_file)])
    assert [json.loads(line)["name"] for line in out_file.read_text().splitlines()] == ["a"]


def reviewing_ctx_with_cost(tmp_path, findings, cost_field):
    """A review whose JSON result carries `cost_field` as total_cost_usd (the key is left out when it is `...`)."""
    result = {"type": "result", "is_error": False, "result": "ok"}
    if cost_field is not ...:
        result["total_cost_usd"] = cost_field
    ok = json.dumps(result)
    probe = json.dumps({"type": "result", "is_error": False, "result": "ok", "total_cost_usd": 0.0})
    fake = FakeRunner().on(*PROBE, stdout=probe).on_prefix("env", stdout=ok)
    return _ctx(tmp_path, ReviewingRunner(fake, findings))


@pytest.mark.parametrize("reported", [..., None, "free", True, -1.0, float("nan"), float("inf")])
def test_a_cost_that_is_missing_or_not_a_real_number_is_unpriced_not_free(tmp_path, source, reported):
    ctx = reviewing_ctx_with_cost(tmp_path, [FOUND], reported)
    out = run_cases(ctx, [make_case(source, name="a"), make_case(source, name="b")])
    assert [c["name"] for c in out["cases"]] == ["a", "b"] and out["unpriced"] == ["a", "b"]  # no budget: all run
    assert out["cases"][0]["claude"]["cost_usd"] is None and out["cost_usd"] == 0.0


def test_with_a_budget_nothing_new_starts_after_an_unpriced_review(tmp_path, source):
    ctx = reviewing_ctx_with_cost(tmp_path, [FOUND], None)
    out = run_cases(ctx, [make_case(source, name="a"), make_case(source, name="b")], budget_usd=5.0)
    assert [c["name"] for c in out["cases"]] == ["a"] and out["skipped_for_budget"] == ["b"] and out["unpriced"] == ["a"]


def test_a_case_that_fails_to_build_is_not_counted_as_an_unpriced_review(tmp_path, source):
    ctx, _ = reviewing_ctx(tmp_path, [FOUND])
    out = run_cases(ctx, [make_case(source, name="bad", head="0" * 40), make_case(source, name="good")], budget_usd=5.0)
    assert [c["name"] for c in out["cases"]] == ["bad", "good"] and out["unpriced"] == []


@pytest.mark.parametrize("labels, text", [
    ({"id": "a"}, "expected a list"),
    (["x"], "expected an object"),
    ([{"id": "a", "round": 1, "path": "f"}], "missing needle"),
    ([{"id": "a", "round": 1, "path": "f", "needle": ""}], "non-empty strings"),
    ([{"id": 5, "round": 1, "path": "f", "needle": "n"}], "non-empty strings"),
    ([{"id": "a", "round": 1, "path": "f", "needle": "n", "occurrence": "2"}], "positive integer"),
    ([{"id": "a", "round": 1, "path": "f", "needle": "n", "occurrence": 0}], "positive integer"),
    ([{"id": "a", "round": 1, "path": "f", "needle": "n"}] * 2, "duplicate ids"),
    ([{"id": "a", "round": "2a", "path": "f", "needle": "n"}], "round must be an integer"),
    ([{"id": "a", "round": True, "path": "f", "needle": "n"}], "round must be an integer"),
])
def test_bad_labels_are_refused_with_a_message(labels, text):
    with pytest.raises(CodeCheckError, match=text):
        replay.validate_labels(labels)


def test_from_git_reports_a_bad_labels_file_as_json_and_accepts_a_relative_clone(tmp_path, reviewed_twice, monkeypatch, capsys):
    r = reviewed_twice
    monkeypatch.setattr(replay, "_context", lambda: _ctx(tmp_path, LiveRunner(FakeRunner())))
    args = ["from-git", "--repo", "o/r", "--base", r["base"], "--labels"]
    for content in (b"\xff\xfe", b"{not json", b'{"id": "a"}', b'["x"]'):
        bad = tmp_path / "bad.json"
        bad.write_bytes(content)
        assert replay.main([*args, str(bad), "--clone", str(r["path"]), r["first"]]) == 1
        assert "error" in json.loads(capsys.readouterr().out)
    good = tmp_path / "labels.json"
    good.write_text(json.dumps(r["labels"]))
    monkeypatch.chdir(r["path"].parent)
    assert replay.main([*args, str(good), "--clone", r["path"].name, r["first"]]) == 0
    case = json.loads(capsys.readouterr().out)[0]
    assert case["clone"] == str(r["path"].resolve())
    validate_case(case)


def test_an_unwritable_raw_dir_does_not_throw_away_a_paid_review(tmp_path, source):
    ctx, _ = reviewing_ctx(tmp_path, [FOUND])
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    out = run_case(ctx, make_case(source), workdir(tmp_path), raw_dir=blocker)
    assert "raw output not saved" in out["raw_error"] and out["score"]["recall"] == 1.0 and "error" not in out


def test_every_finding_near_a_comment_counts_as_on_a_comment():
    findings = [{"file": "calc.py", "line": 10}, {"file": "calc.py", "line": 11}, {"file": "calc.py", "line": 12},
                {"file": "calc.py", "line": 90}]
    out = score(findings, [comment(10), comment(11)])
    assert (out["matched"], out["findings_on_a_comment"], out["findings_elsewhere"]) == (2, 3, 1)


def test_a_finding_without_an_integer_line_lands_on_no_comment_and_does_not_raise():
    findings = [{"file": "calc.py", "line": None}, {"file": "calc.py"}, {"file": "calc.py", "line": "2"},
                {"file": "calc.py", "line": 2}]
    out = score(findings, [comment(2)])
    assert (out["matched"], out["findings_on_a_comment"], out["findings_elsewhere"]) == (1, 1, 3)


@pytest.mark.parametrize("flag, value", [("--budget", "nan"), ("--budget", "inf"), ("--budget", "-1"), ("--limit", "-1"),
                                         ("--tolerance", "-1"), ("--max-per-author", "-2"), ("--limit", "x")])
def test_caps_that_would_disable_or_invert_themselves_are_refused_at_parse_time(tmp_path, flag, value):
    with pytest.raises(SystemExit):
        replay.main(["run", str(tmp_path / "cases.json"), flag, value])


def test_an_unwritable_out_path_fails_before_any_paid_review(tmp_path, source, monkeypatch, capsys):
    ctx = _ctx(tmp_path, LiveRunner(FakeRunner()))
    monkeypatch.setattr(replay, "_context", lambda: ctx)
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([make_case(source)]))
    monkeypatch.setattr(replay, "run_cases", lambda *a, **k: pytest.fail("a review was started"))
    assert replay.main(["run", str(cases), "--out", str(tmp_path / "no" / "dir" / "r.jsonl")]) == 1
    assert "error" in json.loads(capsys.readouterr().out)


def test_a_comment_author_that_is_not_a_string_does_not_raise_in_score():
    comments = [{"path": "a.py", "line": 3, "author": ["x"]}, {"path": "a.py", "line": 9, "author": {"k": 1}}]
    assert score([{"file": "a.py", "line": 3}], comments)["comments"] == 2


def test_anchor_recall_ignores_sites_a_message_merely_names():
    finding = {"file": "a.py", "line": 50, "message": "same flaw at a.py:3"}
    out = score([finding], [comment(3, path="a.py")])
    assert (out["matched"], out["anchor_matched"], out["anchor_recall"]) == (1, 0, 0.0)


def test_score_does_not_raise_on_rounds_of_mixed_types():
    comments = [{**comment(3), "round": 1}, {**comment(9), "round": "2a"}]
    assert sorted(score([], comments)["by_round"], key=str) == [1, "2a"]


def test_a_message_with_a_huge_number_after_a_colon_does_not_raise_in_score():
    finding = {"file": "a.py", "line": 3, "message": "see a.py:" + "9" * 5000}
    assert score([finding], [comment(3, path="a.py")])["matched"] == 1


@pytest.mark.parametrize("where", ["base", "head", "introduced_in", "fixed_in"])
def test_from_git_refuses_a_commit_that_would_read_as_an_option(tmp_path, where):
    label = {"id": "a", "round": 1, "path": "f", "needle": "n"}
    base, heads = "a" * 40, ["b" * 40]
    if where == "base":
        base = "--output=x"
    elif where == "head":
        heads = ["-x"]
    else:
        label[where] = "-y"
    with pytest.raises(CodeCheckError, match="does not start with"):
        replay.cases_from_git(_ctx(tmp_path, LiveRunner(FakeRunner())), tmp_path, "o/r", base, [label], heads)


def test_a_failed_claude_run_keeps_its_error_when_scoring_also_fails(tmp_path, source, monkeypatch):
    probe = json.dumps({"type": "result", "is_error": False, "result": "ok", "total_cost_usd": 0.0})
    bad = json.dumps({"type": "result", "is_error": True, "result": "boom", "total_cost_usd": 0.01})
    fake = FakeRunner().on(*PROBE, stdout=probe).on_prefix("env", stdout=bad)
    ctx = _ctx(tmp_path, ReviewingRunner(fake, [FOUND]))
    monkeypatch.setattr(replay, "score", lambda *a, **k: 1 / 0)
    out = run_case(ctx, make_case(source), workdir(tmp_path))
    assert out["error"].startswith("claude run failed") and out["claude"]["cost_usd"] == 0.01


def test_a_route_named_by_a_short_path_lands_on_the_comment_in_that_file():
    comments = [comment(41, path="src/pkg/execute/plugin.py"), comment(41, path="src/pkg/other.py")]
    findings = [{"file": "src/pkg/execute/plugin.py", "line": 5, "message": "also reaches the error text (plugin.py:41)"}]
    assert [r["matched"] for r in score(findings, comments)["per_comment"]] == [True, False]
    named_with_dirs = [{"file": "x.py", "line": 1, "message": "see execute/plugin.py:41"}]
    assert score(named_with_dirs, comments)["matched"] == 1
    not_a_suffix = [{"file": "x.py", "line": 1, "message": "see myplugin.py:41"}]
    assert score(not_a_suffix, comments)["matched"] == 0
