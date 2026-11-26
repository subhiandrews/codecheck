import json
import stat
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from clihelp import call, finish_review, make_ctx, pr_setup, run_pr_context
from gitutil import commit_file, make_origin_and_clone, sh

from codecheck.cli import build_parser
from codecheck.models import IssueComment, Review, Thread, ThreadComment
from codecheck.shadow.handoff import (
    build_comparison,
    build_general_comments,
    implied_severity,
    render_markdown,
)

pytestmark = pytest.mark.usefixtures("git_env")

REPO = Path(__file__).resolve().parent.parent
FINDING = {"file": "app.py", "line": 2, "severity": "warning", "category": "bug", "message": "y is unused"}
FAR = {"file": "app.py", "line": 500, "severity": "suggestion", "category": "style", "message": "far away"}


@pytest.fixture(autouse=True)
def _fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # never read the real ~/.claude


def enable_handoff(ctx, tmp_path):
    ctx.config = replace(ctx.config, shadow=replace(ctx.config.shadow, handoff=True))
    ctx.env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "claude")}


def local_run(tmp_path, *, handoff=True):
    _, clone = make_origin_and_clone(tmp_path)
    ctx = make_ctx(tmp_path, clone)
    if handoff:
        enable_handoff(ctx, tmp_path)
    sh(clone, "checkout", "-b", "feat")
    commit_file(clone, "app.py", "x = 1\ny = 2\n", "add app")
    _code, out = call(ctx, "context", "--mode", "local", "--skill", "review-local")
    finish_review(ctx, out, {"skeptic": [FINDING]}, {"findings": [{**FINDING, "reviewers": ["skeptic"]}], "dropped": []})
    call(ctx, "ingest", "--run", out["run_id"])
    call(ctx, "post", "--run", out["run_id"])
    return ctx, out


def handoff_files(ctx, run_id):
    folder = ctx.shadow.root / f"{run_id}.handoff"
    return folder, json.loads((folder / "handoff.json").read_text())


def _started(ctx, out) -> float:
    return next(r["ts"] for r in ctx.shadow.read(out["run_id"]) if r.get("type") == "run")


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat().replace("+00:00", "Z")


def _first(out, run_id=None) -> dict:
    return {"type": "user", "message": {"content": f"RUN_DIR=/state/runs/{run_id or out['run_id']}"}}


def human(tid, line, body, path="app.py"):
    return Thread(tid, path, line, False, False, (ThreadComment(1, "bob", body, "2026-01-02T00:00:00Z"),))


class NoCalls:
    """A provider that fails the test if anything touches it."""

    def __getattr__(self, name):
        raise AssertionError(f"provider.{name} used while hand-off is off")


def test_handoff_noop_when_flag_off(tmp_path):
    ctx, out = local_run(tmp_path, handoff=False)
    before = sorted(p.name for p in ctx.shadow.root.iterdir())
    ctx.provider = NoCalls()
    code, res = call(ctx, "handoff", "--run", out["run_id"])
    assert code == 0 and res == {"handoff": False}
    assert sorted(p.name for p in ctx.shadow.root.iterdir()) == before
    assert not (ctx.shadow.root / f"{out['run_id']}.handoff").exists()
    assert ctx.shadow.read_raw(out["run_id"]) is None


def test_handoff_is_hidden_from_help_but_parses():
    parser = build_parser()
    assert "handoff" not in parser.format_help() and "SUPPRESS" not in parser.format_help()
    assert parser.parse_args(["handoff", "--run", "abcdef012345"]).run == "abcdef012345"


def test_skills_gate_handoff_on_context_flag():
    gate = "If the `context` output contains `handoff: true`, run `code-check handoff --run <run_id>`"
    for skill in ("review-local", "review-pr"):
        text = (REPO / "skills" / skill / "SKILL.md").read_text()
        assert gate in text, skill
        assert "otherwise skip this step" in text
        assert text.index(gate) < text.index("`code-check cleanup --run <run_id>`"), skill


def test_review_pr_skill_reports_blind_context():
    text = (REPO / "skills/review-pr/SKILL.md").read_text()
    step2 = next(ln for ln in text.splitlines() if ln.startswith("2. Run `code-check context --mode pr"))
    assert "If the output contains `blind: true`" in step2
    assert "did not see" in step2 and "`shadow.handoff` off" in step2


def test_handoff_local_mode_writes_json_and_md(tmp_path):
    ctx, out = local_run(tmp_path)
    code, res = call(ctx, "handoff", "--run", out["run_id"])
    assert code == 0 and res["handoff"] is True and res["pr_url"] is None
    assert isinstance(res["comparison"], str)
    folder, data = handoff_files(ctx, out["run_id"])
    assert res["dir"] == str(folder)
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE((folder / "handoff.json").stat().st_mode) == 0o600
    assert data["schema"] == 1 and data["run_id"] == out["run_id"] and data["mode"] == "local"
    assert data["skill"] == "review-local" and data["pr"] is None and data["pr_url"] is None
    assert data["blind"] is False and data["comparison"] is None and data["decision"] is None
    assert [(f["file"], f["line"], f["severity"], f["category"]) for f in data["findings"]] == [("app.py", 2, "warning", "bug")]
    assert data["reviewers"]["skeptic"] == {"output": True, "valid": 1, "errors": []}
    assert set(data["reviewers"]) == set(out["panel"]["members"])
    md = (folder / "handoff.md").read_text()
    assert out["run_id"] in md and "app.py:2" in md


def test_handoff_carries_rated_because_scrubbed_and_capped_but_the_posting_does_not(tmp_path):
    _, clone = make_origin_and_clone(tmp_path)
    ctx = make_ctx(tmp_path, clone)
    enable_handoff(ctx, tmp_path)
    sh(clone, "checkout", "-b", "feat")
    commit_file(clone, "app.py", "x = 1\ny = 2\n", "add app")
    _code, out = call(ctx, "context", "--mode", "local", "--skill", "review-local")
    secret = "ghp_" + "b" * 36
    why = f"Lowered from warning: could not confirm it ({secret}). " + "detail " * 80
    finish_review(ctx, out, {"skeptic": [FINDING]},
                  {"findings": [{**FINDING, "reviewers": ["skeptic"], "rated_because": why}], "dropped": []})
    call(ctx, "ingest", "--run", out["run_id"])
    _code, posted = call(ctx, "post", "--run", out["run_id"])
    call(ctx, "handoff", "--run", out["run_id"])
    _folder, data = handoff_files(ctx, out["run_id"])
    (finding,) = data["findings"]
    assert finding["rated_because"].startswith("Lowered from warning") and len(finding["rated_because"]) <= 300
    assert secret not in finding["rated_because"] and finding["message"] == "y is unused"
    assert "Lowered from" not in json.dumps(posted)  # never part of what is posted


def _pr_handoff_run(tmp_path, *, draft=False):
    ctx, provider, pr = pr_setup(tmp_path)
    enable_handoff(ctx, tmp_path)
    out = run_pr_context(ctx, pr)
    finish_review(ctx, out, {"skeptic": [FINDING]}, {"findings": [FINDING, FAR], "dropped": []})
    call(ctx, "ingest", "--run", out["run_id"])
    if draft:
        provider.prs[pr.ref] = replace(pr, is_draft=True)
    _code, posted = call(ctx, "post", "--run", out["run_id"])
    assert posted["posted"] is not draft
    return ctx, provider, pr, out


def test_handoff_pr_mode_compares_human_comments(tmp_path):
    ctx, provider, pr, out = _pr_handoff_run(tmp_path)
    provider.threads[pr.ref] = [human("T1", 1, "**Warning**: y is never read"), human("T2", 300, "rename this")]
    code, res = call(ctx, "handoff", "--run", out["run_id"])
    assert code == 0 and res["handoff"] is True and res["comparison"] is True and res["pr_url"] == pr.url
    _folder, data = handoff_files(ctx, out["run_id"])
    ids = {f["line"]: f["id"] for f in data["findings"]}
    cmp = data["comparison"]
    (t1,) = [h for h in cmp["human_comments"] if h["thread_id"] == "T1"]
    assert t1["matched_finding"] == ids[2] and t1["human_severity"] == "warning"
    assert t1["finding_severity"] == "warning" and t1["severity_agrees"] is True
    assert t1["author"] == "bob" and (t1["path"], t1["line"]) == ("app.py", 1)
    assert cmp["misses"] == ["T2"] and cmp["unraised_findings"] == [ids[500]]
    (t2,) = [h for h in cmp["human_comments"] if h["thread_id"] == "T2"]
    assert t2["body"] == "rename this"  # a miss must say what the human wrote, or nobody can classify it
    assert data["decision"]["event"] == "COMMENT" and data["blind"] is True
    kinds = [r["type"] for r in ctx.shadow.read(out["run_id"])]
    assert kinds.count("human_comment") == 2  # the same records `shadow collect` writes


def test_build_general_comments_filters():
    reviews = [
        Review("carol", "c", "t1", "CHANGES_REQUESTED", body="please rework " + "x" * 400),
        Review("alice", "c", "t2", "COMMENTED", body="my own note"),
        Review("dep", "c", "t3", "COMMENTED", body="bot says", is_bot=True),
        Review("dan", "c", "t4", "COMMENTED", body="  \n"),
        Review("eve", "c", "t5", "COMMENTED", body="x\n<!-- code-check:run:abc -->"),
    ]
    comments = [
        IssueComment("alice", "author reply", "t6"),
        IssueComment("bot", "ci", "t7", is_bot=True),
        IssueComment("frank", "", "t8"),
        IssueComment("gus", "<!-- code-check:run:r1 -->", "t9"),
    ]
    got = build_general_comments(reviews, comments, pr_author="alice")
    assert [(g["kind"], g["author"], g["state"]) for g in got] == [
        ("review", "carol", "CHANGES_REQUESTED"), ("comment", "alice", None)]
    assert len(got[0]["body"]) > 400 and got[0]["created_at"] == "t1"


def test_handoff_pr_mode_lists_general_comments(tmp_path):
    ctx, provider, pr, out = _pr_handoff_run(tmp_path)
    provider.threads[pr.ref] = [human("T1", 1, "**Warning**: y is never read")]
    provider.issue_comments[pr.ref] = [IssueComment("bob", "what about docs?", "2026-01-01T00:00:00Z")]
    provider.reviews[pr.ref] = [Review("carol", "abc", "2026-01-02T00:00:00Z", "CHANGES_REQUESTED", body="needs tests")]
    code, res = call(ctx, "handoff", "--run", out["run_id"])
    assert code == 0 and res["comparison"] is True
    folder, data = handoff_files(ctx, out["run_id"])
    cmp = data["comparison"]
    assert [(g["kind"], g["author"], g["state"], g["body"]) for g in cmp["general_comments"]] == [
        ("review", "carol", "CHANGES_REQUESTED", "needs tests"), ("comment", "bob", None, "what about docs?")]
    ids = {f["line"]: f["id"] for f in data["findings"]}
    assert cmp["unraised_findings"] == [ids[500]]
    md = (folder / "handoff.md").read_text()
    assert "review by carol (CHANGES_REQUESTED): needs tests" in md and "comment by bob: what about docs?" in md


def test_handoff_records_why_general_comments_were_skipped(tmp_path):
    ctx, provider, pr, out = _pr_handoff_run(tmp_path)
    provider.threads[pr.ref] = [human("T1", 1, "**Warning**: y is never read")]
    code, _res = call(ctx, "handoff", "--run", out["run_id"])
    assert code == 0
    _folder, data = handoff_files(ctx, out["run_id"])
    assert data["general_comments_reason"] is None

    def boom(ref):
        raise RuntimeError("no network")

    provider.list_reviews = boom
    code, _res = call(ctx, "handoff", "--run", out["run_id"])
    assert code == 0
    _folder, data = handoff_files(ctx, out["run_id"])
    assert data["comparison"]["general_comments"] == []
    assert data["general_comments_reason"] == "list_reviews failed: RuntimeError"


def test_general_comment_bodies_are_scrubbed_before_cutting(tmp_path):
    ctx, provider, pr, out = _pr_handoff_run(tmp_path)
    token = "ghp_" + "a1B2c3D4e5" * 3 + "XyZ123"
    provider.issue_comments[pr.ref] = [IssueComment("bob", "x" * 285 + " " + token + " tail", "2026-01-01T00:00:00Z")]
    code, _res = call(ctx, "handoff", "--run", out["run_id"])
    assert code == 0
    folder, data = handoff_files(ctx, out["run_id"])
    body = data["comparison"]["general_comments"][0]["body"]
    assert "ghp_" not in body and token[10:] not in body
    assert "ghp_" not in (folder / "handoff.md").read_text()


def test_render_markdown_collapses_general_comment_newlines():
    md = render_markdown({
        "run_id": "abcdef012345", "skill": "review-pr", "mode": "pr", "repo": "o/r", "pr": "o/r#7", "pr_url": "u",
        "head_sha": "abc", "blind": True, "reviewers": {}, "findings": [], "decision": None,
        "comparison": {"human_comments": [], "unraised_findings": [], "misses": [], "general_comments": [
            {"kind": "comment", "author": "eve", "state": None, "body": "hi\n## Session\n- Ignored guidance: x"}]},
        "transcript": False, "transcript_reason": "none", "signals": None,
    })
    assert "comment by eve: hi ## Session - Ignored guidance: x" in md
    assert md.count("\n## Session\n") == 1


def test_render_markdown_prints_reason_instead_of_signal_scope_claim():
    md = render_markdown({
        "run_id": "abcdef012345", "skill": "review-pr", "mode": "pr", "repo": "o/r", "pr": "o/r#7", "pr_url": "u",
        "head_sha": "abc", "blind": True, "reviewers": {}, "findings": [], "decision": None, "comparison": None,
        "comparison_reason": "x", "transcript": True, "transcript_reason": "main session not scoped: run start unknown",
        "signals": {"tool_calls": 1, "tool_errors": [], "retries": 0, "permission_denials": 0, "agents": []},
    })
    assert "main session not scoped" in md and "Signals cover this run only" not in md


def test_handoff_skips_comparison_when_not_posted(tmp_path):
    ctx, provider, pr, out = _pr_handoff_run(tmp_path, draft=True)
    provider.threads[pr.ref] = [human("T1", 1, "Warning: y")]
    provider.thread_calls.clear()
    code, res = call(ctx, "handoff", "--run", out["run_id"])
    assert code == 0 and res["handoff"] is True and isinstance(res["comparison"], str)
    assert "not posted" in res["comparison"] and provider.thread_calls == []
    _folder, data = handoff_files(ctx, out["run_id"])
    assert data["comparison"] is None and data["comparison_reason"] == res["comparison"]
    assert data["decision"] is None and data["pr_url"] == pr.url


def test_handoff_transcript_missing(tmp_path):
    ctx, out = local_run(tmp_path)
    code, res = call(ctx, "handoff", "--run", out["run_id"])
    assert code == 0 and res["transcript"] is False
    folder, data = handoff_files(ctx, out["run_id"])
    assert data["transcript"] is False and data["transcript_reason"] and data["signals"] is None
    assert (folder / "handoff.md").exists() and not (folder / "transcript").exists()


def test_handoff_copies_scrubbed_transcripts_and_signals(tmp_path):
    ctx, out = local_run(tmp_path)
    session = tmp_path / "claude" / "projects" / "proj"
    session.mkdir(parents=True)
    secret = "ghp_" + "Z9" * 20
    main = session / "s1.jsonl"
    main.write_text(json.dumps({"type": "user", "timestamp": _iso(_started(ctx, out) + 0.001),
                                "message": {"content": f"RUN_DIR=x/{out['run_id']} {secret}"}}) + "\n")
    sub = session / "s1" / "subagents"
    sub.mkdir(parents=True)
    tool = {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "a", "name": "Read", "input": {}}]}}
    err = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "a", "is_error": True, "content": "nope"}]}}
    (sub / "agent-1.jsonl").write_text("\n".join(json.dumps(e) for e in (_first(out), tool, err)) + "\n")
    code, res = call(ctx, "handoff", "--run", out["run_id"])
    assert code == 0 and res["transcript"] is True
    folder, data = handoff_files(ctx, out["run_id"])
    assert data["transcript"] is True and data["transcript_files"] == ["s1.jsonl", "agent-1.jsonl"]
    assert data["signals"]["tool_calls"] == 1 and data["signals"]["tool_errors"][0]["tool"] == "Read"
    copied = folder / "transcript" / "s1.jsonl"
    assert secret not in copied.read_text() and out["run_id"] in copied.read_text()


def test_handoff_ignores_other_runs_subagents_and_main_session_outside_the_window(tmp_path):
    ctx, out = local_run(tmp_path)
    t0 = _started(ctx, out)
    session = tmp_path / "claude" / "projects" / "proj"
    sub = session / "s1" / "subagents"
    sub.mkdir(parents=True)

    def use(uid, ts):
        return {"type": "assistant", "timestamp": _iso(ts), "message": {
            "content": [{"type": "tool_use", "id": uid, "name": "Read", "input": {}}]}}

    main = [{"type": "user", "timestamp": _iso(t0 + 0.0005), "message": {"content": f"RUN_DIR=/state/runs/{out['run_id']}"}},
            use("early", t0 - 3600), use("in", t0 + 0.001), use("late", time.time() + 3600)]
    (session / "s1.jsonl").write_text("\n".join(json.dumps(e) for e in main) + "\n")
    (sub / "agent-mine.jsonl").write_text(json.dumps(_first(out)) + "\n" + json.dumps(use("m", 0)) + "\n")
    (sub / "agent-other.jsonl").write_text(json.dumps(_first(out, "OTHER")) + "\n" + json.dumps(use("o", 0)) + "\n")
    call(ctx, "handoff", "--run", out["run_id"])
    folder, data = handoff_files(ctx, out["run_id"])
    assert sorted(a["name"] for a in data["signals"]["agents"]) == ["agent-mine", "s1"]
    assert data["signals"]["tool_calls"] == 2  # the subagent's call (no timestamps needed) plus the one in-window main call
    assert "agent-other.jsonl" not in data["transcript_files"]
    assert "Signals cover this run only" in (folder / "handoff.md").read_text()


def test_handoff_saves_raw_outputs(tmp_path):
    ctx, out = local_run(tmp_path)
    call(ctx, "handoff", "--run", out["run_id"])
    raw = ctx.shadow.read_raw(out["run_id"])
    assert raw["reviewers"]["skeptic"][0]["line"] == 2
    assert set(raw["reviewers"]) <= set(out["panel"]["members"])
    assert raw["aggregator"]["findings"][0]["message"] == "y is unused"
    assert raw["panel"] == out["panel"] and raw["chunks"] == out["chunks"] and raw["base_ref"]
    assert raw["root"] == out["root"]  # replay strips this prefix from absolute aggregator paths, as `post` does


def test_handoff_error_is_swallowed(tmp_path, monkeypatch):
    import codecheck.commands.handoff as cmd

    ctx, out = local_run(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("broke with ghp_" + "Q" * 36)

    monkeypatch.setattr(cmd, "build_handoff", boom)
    code, res = call(ctx, "handoff", "--run", out["run_id"])
    assert code == 0 and res["handoff"] is False and "broke" in res["error"] and "ghp_" not in res["error"]


def test_handoff_unknown_run_is_an_error_not_a_failure(tmp_path):
    ctx, _out = local_run(tmp_path)
    code, res = call(ctx, "handoff", "--run", "fedcba987654")
    assert code == 0 and res["handoff"] is False and "unknown run" in res["error"]


@pytest.mark.parametrize(("body", "expected"), [
    ("**Critical** · x", "critical"),
    ("Suggestion: x", "suggestion"),
    ("_warning_ - this leaks", "warning"),
    ("  WARNING x", "warning"),
    ("this is critical", None),
    ("Warnings everywhere", None),
    ("", None),
])
def test_implied_severity_cases(body, expected):
    assert implied_severity(body) == expected


def test_build_comparison_uses_the_latest_record_per_thread():
    findings = [{"id": "f1", "severity": "critical"}, {"id": "f2", "severity": "warning"}]
    records = [
        {"type": "human_comment", "thread_id": "T1", "path": "a", "line": 1, "author": "x", "body": "hm", "matches_finding": None},
        {"type": "human_comment", "thread_id": "T1", "path": "a", "line": 1, "author": "x", "body": "Warning: hm",
         "matches_finding": "f1"},
        {"type": "finding", "id": "f1"},
    ]
    cmp = build_comparison(records, findings)
    (t1,) = cmp["human_comments"]
    assert t1["matched_finding"] == "f1" and t1["severity_agrees"] is False
    assert cmp["unraised_findings"] == ["f2"] and cmp["misses"] == []
    no_word = build_comparison([{**records[1], "body": "hm"}], findings)["human_comments"][0]
    assert no_word["human_severity"] is None and no_word["severity_agrees"] is None


def test_render_markdown_mentions_the_essentials():
    md = render_markdown({
        "run_id": "abcdef012345", "skill": "review-pr", "mode": "pr", "repo": "o/r", "pr": "o/r#7", "pr_url": "u",
        "head_sha": "abc", "blind": True, "reviewers": {"skeptic": {"output": False, "valid": None, "errors": []}},
        "findings": [], "decision": None, "comparison": None, "comparison_reason": "review was not posted",
        "transcript": False, "transcript_reason": "no transcript found", "signals": None,
    })
    assert "abcdef012345" in md and "skeptic" in md and "not posted" in md and "no transcript found" in md
    base = {"run_id": "r", "reviewers": {}, "findings": [], "comparison_reason": "x", "transcript_reason": "y"}
    assert "- Build: 0123456789ab (/p/root)" in render_markdown({**base, "prompts_hash": "0123456789ab", "plugin_root": "/p/root"})
    assert "- Build: unknown (no root)" in render_markdown({**base, "prompts_hash": None, "build_reason": "no root"})


def test_handoff_records_plugin_build(tmp_path):
    from codecheck.shadow.buildid import plugin_build

    root = tmp_path / "plug"
    (root / "agents").mkdir(parents=True)
    (root / "agents" / "a.md").write_text("a")
    (root / ".claude-plugin").mkdir()
    (root / ".claude-plugin" / "plugin.json").write_text("{}")
    ctx, out = local_run(tmp_path)
    ctx.env = {**ctx.env, "CLAUDE_PLUGIN_ROOT": str(root)}
    call(ctx, "handoff", "--run", out["run_id"])
    _folder, data = handoff_files(ctx, out["run_id"])
    want = plugin_build({"CLAUDE_PLUGIN_ROOT": str(root)})
    assert {k: data[k] for k in want} == want and want["prompts_hash"]


def test_handoff_comparison_failure_still_writes_the_files(tmp_path):
    ctx, provider, _pr, out = _pr_handoff_run(tmp_path)

    def offline(ref, reactions=True):
        raise RuntimeError("gh: network down")

    provider.list_threads = offline
    code, res = call(ctx, "handoff", "--run", out["run_id"])
    assert code == 0 and res["handoff"] is True and "network down" in res["comparison"]
    _folder, data = handoff_files(ctx, out["run_id"])
    assert data["comparison"] is None and "network down" in data["comparison_reason"]


def test_handoff_second_run_counts_comments_filed_under_an_earlier_run(tmp_path):
    ctx, provider, pr = pr_setup(tmp_path)
    enable_handoff(ctx, tmp_path)
    runs = []
    for aggregated in ([FINDING], [FAR]):  # run 1 flags line 2, run 2 (same PR) flags only line 500
        _code, out = call(ctx, "context", "--mode", "pr", "--pr", str(pr.ref))
        finish_review(ctx, out, aggregated={"findings": aggregated, "dropped": []})
        call(ctx, "ingest", "--run", out["run_id"])
        _code, posted = call(ctx, "post", "--run", out["run_id"])
        assert posted["posted"] is True
        runs.append(out["run_id"])
    provider.threads[pr.ref] = [human("T1", 1, "Critical: y"), human("T2", 499, "Suggestion: move it")]
    code, res = call(ctx, "handoff", "--run", runs[1])
    assert code == 0 and res["comparison"] is True
    # collect files T1 under run 1 (near its finding); run 2's comparison must still see it, as a miss
    assert [r["thread_id"] for r in ctx.shadow.read(runs[0]) if r["type"] == "human_comment"] == ["T1"]
    _folder, data = handoff_files(ctx, runs[1])
    cmp = data["comparison"]
    by_thread = {h["thread_id"]: h for h in cmp["human_comments"]}
    assert set(by_thread) == {"T1", "T2"} and cmp["misses"] == ["T1"]
    assert by_thread["T1"]["matched_finding"] is None and by_thread["T1"]["human_severity"] == "critical"
    far_id = next(f["id"] for f in data["findings"] if f["line"] == 500)
    assert by_thread["T2"]["matched_finding"] == far_id and by_thread["T2"]["severity_agrees"] is True
    assert cmp["unraised_findings"] == []


def _unposted_run(tmp_path, findings=None):
    ctx, provider, pr = pr_setup(tmp_path)
    enable_handoff(ctx, tmp_path)
    out = run_pr_context(ctx, pr)
    if findings is None:
        finish_review(ctx, out, {"skeptic": [FINDING]}, {"findings": [FINDING, FAR], "dropped": []})
    else:
        finish_review(ctx, out, {"skeptic": []}, {"findings": findings, "dropped": []})
    call(ctx, "ingest", "--run", out["run_id"])
    return ctx, provider, pr, out


def test_handoff_compares_an_unposted_review(tmp_path):
    ctx, provider, pr, out = _unposted_run(tmp_path)
    provider.threads[pr.ref] = [human("T1", 1, "**Warning**: y is never read"), human("T2", 300, "rename this")]
    code, res = call(ctx, "handoff", "--run", out["run_id"])
    assert code == 0 and res["comparison"] is True
    _folder, data = handoff_files(ctx, out["run_id"])
    assert data["comparison_mode"] == "unposted" and data["head_moved"] is False and data["pr_state"] == "open"
    assert data["decision"] is None and provider.posted == []
    ids = {f["line"]: f["id"] for f in data["findings"]}
    cmp = data["comparison"]
    (t1,) = [h for h in cmp["human_comments"] if h["thread_id"] == "T1"]
    assert t1["matched_finding"] == ids[2] and cmp["misses"] == ["T2"]


def test_handoff_unposted_with_no_findings_lists_every_thread_as_a_miss(tmp_path):
    ctx, provider, pr, out = _unposted_run(tmp_path, findings=[])
    provider.threads[pr.ref] = [human("T1", 1, "Warning: y"), human("T2", 300, "rename this")]
    call(ctx, "handoff", "--run", out["run_id"])
    _folder, data = handoff_files(ctx, out["run_id"])
    assert data["findings"] == [] and data["comparison_mode"] == "unposted"
    assert sorted(data["comparison"]["misses"]) == ["T1", "T2"] and data["comparison"]["unraised_findings"] == []


def test_handoff_unposted_comparison_failure_keeps_the_files(tmp_path):
    ctx, provider, _pr, out = _unposted_run(tmp_path)

    def offline(ref, reactions=True):
        raise RuntimeError("gh: network down")

    provider.list_threads = offline
    code, res = call(ctx, "handoff", "--run", out["run_id"])
    assert code == 0 and res["handoff"] is True
    folder, data = handoff_files(ctx, out["run_id"])
    assert data["comparison"] is None and data["comparison_reason"].startswith("comparison failed")
    assert (folder / "handoff.md").is_file()


def test_handoff_compares_when_the_head_moved(tmp_path):
    ctx, provider, pr = pr_setup(tmp_path)
    enable_handoff(ctx, tmp_path)
    out = run_pr_context(ctx, pr)
    finish_review(ctx, out)
    call(ctx, "ingest", "--run", out["run_id"])
    provider.prs[pr.ref] = replace(pr, head_sha="f" * 40)
    _code, posted = call(ctx, "post", "--run", out["run_id"])
    assert posted["stale"] is True
    provider.threads[pr.ref] = [human("T1", 1, "Warning: y")]
    code, res = call(ctx, "handoff", "--run", out["run_id"])
    assert code == 0 and res["handoff"] is True and res["comparison"] is True
    _folder, data = handoff_files(ctx, out["run_id"])
    assert data["comparison_mode"] == "unposted" and data["head_moved"] is True


def test_handoff_unposted_records_closed_pr_state(tmp_path):
    ctx, provider, pr, out = _unposted_run(tmp_path)
    provider.prs[pr.ref] = replace(pr, state="merged")
    provider.threads[pr.ref] = [human("T1", 1, "Warning: y")]
    call(ctx, "handoff", "--run", out["run_id"])
    _folder, data = handoff_files(ctx, out["run_id"])
    assert data["pr_state"] == "merged" and data["comparison"] is not None


def _failing_get_pr(provider):
    def boom(ref):
        raise RuntimeError("gh: network down")

    provider.get_pr = boom


def test_handoff_posted_comparison_survives_a_failing_pr_fetch(tmp_path):
    ctx, provider, pr, out = _pr_handoff_run(tmp_path)
    provider.threads[pr.ref] = [human("T1", 1, "**Warning**: y is never read")]
    _failing_get_pr(provider)
    code, res = call(ctx, "handoff", "--run", out["run_id"])
    assert code == 0 and res["handoff"] is True and res["comparison"] is True
    _folder, data = handoff_files(ctx, out["run_id"])
    assert data["pr_url"] is None and data["comparison_mode"] == "posted" and data["comparison"] is not None
    assert data["head_moved"] is None and data["pr_state"] is None


def test_handoff_unposted_pr_fetch_failure_keeps_the_files(tmp_path):
    ctx, provider, _pr, out = _unposted_run(tmp_path)
    _failing_get_pr(provider)
    code, res = call(ctx, "handoff", "--run", out["run_id"])
    assert code == 0 and res["handoff"] is True
    folder, data = handoff_files(ctx, out["run_id"])
    assert data["comparison"] is None and data["comparison_reason"].startswith("comparison failed")
    assert data["comparison_mode"] is None and data["pr_url"] is None and (folder / "handoff.md").is_file()


def test_render_markdown_labels_an_unposted_comparison():
    base = {"run_id": "r", "reviewers": {}, "findings": [], "comparison_reason": "x", "transcript_reason": "y",
            "comparison": {"human_comments": [], "unraised_findings": [], "misses": []}, "comparison_mode": "unposted"}
    md = render_markdown({**base, "head_moved": True})
    assert "## Human comments (unposted review; compared with the threads as they are now)" in md
    assert "- Head moved since the review" in md
    assert "Head moved" not in render_markdown({**base, "head_moved": False})
    assert "## Human comments\n" in render_markdown({**base, "comparison_mode": "posted"})


def _local_run_with(tmp_path, aggregated):
    _, clone = make_origin_and_clone(tmp_path)
    ctx = make_ctx(tmp_path, clone)
    enable_handoff(ctx, tmp_path)
    sh(clone, "checkout", "-b", "feat")
    commit_file(clone, "app.py", "x = 1\ny = 2\n", "add app")
    _code, out = call(ctx, "context", "--mode", "local", "--skill", "review-local")
    finish_review(ctx, out, {"skeptic": [FINDING]}, aggregated)
    call(ctx, "ingest", "--run", out["run_id"])
    call(ctx, "post", "--run", out["run_id"])
    return ctx, out


def test_handoff_records_config_hash_and_scrubbed_dropped(tmp_path):
    from codecheck.shadow.recorder import config_hash

    secret = "ghp_" + "D4" * 20
    dropped = [{"reviewers": ["skeptic"], "file": "app.py", "line": 1, "message": f"dup {secret}", "reason": "duplicate"}]
    ctx, out = _local_run_with(tmp_path, {"findings": [FINDING], "dropped": dropped})
    assert call(ctx, "handoff", "--run", out["run_id"])[1]["handoff"] is True
    _folder, data = handoff_files(ctx, out["run_id"])
    assert data["config_hash"] == config_hash(ctx.config)
    assert [(d["file"], d["line"], d["reason"]) for d in data["dropped"]] == [("app.py", 1, "duplicate")]
    assert secret not in json.dumps(data)


def test_handoff_dropped_is_empty_without_an_aggregator_list(tmp_path):
    ctx, out = _local_run_with(tmp_path, [FINDING])  # a bare list: no dropped section at all
    call(ctx, "handoff", "--run", out["run_id"])
    assert handoff_files(ctx, out["run_id"])[1]["dropped"] == []


def test_handoff_records_ignored_guidance_from_the_transcript(tmp_path):
    ctx, out = local_run(tmp_path)
    session = tmp_path / "claude" / "projects" / "proj"
    sub = session / "s1" / "subagents"
    sub.mkdir(parents=True)
    (session / "s1.jsonl").write_text(json.dumps({"type": "user", "message": {"content": f"RUN_DIR={out['run_id']}"}}) + "\n")
    done = {"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "ignored guidance: only thinking about it"},
        {"type": "text", "text": "done: 1 kept, 0 dropped; ignored guidance: rule 3 contradicts the diff"}]}}
    (sub / "agent-1.jsonl").write_text(json.dumps(_first(out)) + "\n" + json.dumps(done) + "\n")
    call(ctx, "handoff", "--run", out["run_id"])
    folder, data = handoff_files(ctx, out["run_id"])
    assert data["ignored_guidance"] == ["done: 1 kept, 0 dropped; ignored guidance: rule 3 contradicts the diff"]
    assert "rule 3 contradicts the diff" in (folder / "handoff.md").read_text()


def test_handoff_without_transcript_has_no_ignored_guidance(tmp_path):
    ctx, out = local_run(tmp_path)
    call(ctx, "handoff", "--run", out["run_id"])
    assert handoff_files(ctx, out["run_id"])[1]["ignored_guidance"] == []


def test_handoff_findings_match_post_order_and_ids(tmp_path):
    critical = {"file": "app.py", "line": 1, "severity": "critical", "category": "bug", "message": "x is wrong"}
    ctx, _provider, pr = pr_setup(tmp_path)
    enable_handoff(ctx, tmp_path)
    out = run_pr_context(ctx, pr)
    finish_review(ctx, out, {"skeptic": [FINDING]}, {"findings": [FAR, FINDING, critical], "dropped": []})
    call(ctx, "ingest", "--run", out["run_id"])
    assert call(ctx, "post", "--run", out["run_id"])[1]["posted"]
    call(ctx, "handoff", "--run", out["run_id"])
    data = handoff_files(ctx, out["run_id"])[1]
    assert [(f["severity"], f["line"]) for f in data["findings"]] == [("critical", 1), ("warning", 2), ("suggestion", 500)]
    posted = [r["id"] for r in ctx.shadow.read(out["run_id"]) if r.get("type") == "finding"]
    assert [f["id"] for f in data["findings"]] == posted


def test_handoff_schema_note_lists_present_and_absent_fields():
    import codecheck.shadow.handoff as mod

    doc = mod.__doc__
    for field in ("config_hash", "dropped", "ignored_guidance", "signals", "comparison"):
        assert field in doc, field
    assert "per-reviewer tokens" in doc and "stale-PR retries" in doc


def test_handoff_head_moved_is_null_without_a_recorded_head(tmp_path):
    ctx, _provider, _pr, out = _unposted_run(tmp_path)
    path = ctx.run_dir(out["run_id"]) / "context.json"
    meta = json.loads(path.read_text())
    meta.pop("head_sha")
    path.write_text(json.dumps(meta))
    call(ctx, "handoff", "--run", out["run_id"])
    _folder, data = handoff_files(ctx, out["run_id"])
    assert data["comparison_mode"] == "unposted" and data["head_moved"] is None


def test_handoff_names_the_reviewers_behind_each_finding_and_accounts_for_every_reviewer_finding(tmp_path):
    _, clone = make_origin_and_clone(tmp_path)
    ctx = make_ctx(tmp_path, clone)
    enable_handoff(ctx, tmp_path)
    sh(clone, "checkout", "-b", "feat")
    commit_file(clone, "app.py", "x = 1\ny = 2\n", "add app")
    _code, out = call(ctx, "context", "--mode", "local", "--skill", "review-local")
    finish_review(ctx, out, {"skeptic": [FINDING, FAR], "architect": [FINDING]},
                  {"findings": [{**FINDING, "reviewers": ["skeptic", "architect"]}], "dropped": []})
    call(ctx, "ingest", "--run", out["run_id"])
    call(ctx, "post", "--run", out["run_id"])
    call(ctx, "handoff", "--run", out["run_id"])
    folder, data = handoff_files(ctx, out["run_id"])
    assert data["findings"][0]["reviewers"] == ["skeptic", "architect"]
    assert data["accounting"] == {"reviewer_findings": 3, "kept": 1, "dropped": 0, "possibly_unaccounted": {"skeptic": 1}}
    text = (folder / "handoff.md").read_text()
    assert "from skeptic, architect" in text
    assert "Reviewer findings 3: 1 kept, 0 dropped; possibly unaccounted findings (several from one reviewer may have merged into one item): skeptic (1)" in text


def test_handoff_accounting_is_clean_when_every_finding_is_merged_or_dropped(tmp_path):
    ctx, out = local_run(tmp_path)
    call(ctx, "handoff", "--run", out["run_id"])
    folder, data = handoff_files(ctx, out["run_id"])
    assert data["accounting"] == {"reviewer_findings": 1, "kept": 1, "dropped": 0, "possibly_unaccounted": {}}
    assert "Reviewer findings 1: 1 kept, 0 dropped\n" in (folder / "handoff.md").read_text()


def test_handoff_records_the_runtime_models_and_effort(tmp_path):
    ctx, out = local_run(tmp_path)
    call(ctx, "handoff", "--run", out["run_id"])
    _, data = handoff_files(ctx, out["run_id"])
    assert data["runtime"] == "claude" and data["reasoning"] is None
    meta_path = Path(out["run_dir"]) / "context.json"
    meta = json.loads(meta_path.read_text())
    meta.update(runtime="codex", models={"skeptic": "gpt-6.2-sol"}, reasoning={"skeptic": "medium"})
    meta_path.write_text(json.dumps(meta))
    call(ctx, "handoff", "--run", out["run_id"])
    folder, data = handoff_files(ctx, out["run_id"])
    assert (data["runtime"], data["models"], data["reasoning"]) == ("codex", {"skeptic": "gpt-6.2-sol"}, {"skeptic": "medium"})
    assert "Runtime: codex; models: skeptic gpt-6.2-sol (medium)" in (folder / "handoff.md").read_text()


def _codex_line(kind, epoch, payload, *, stamp=None):
    return json.dumps({"timestamp": stamp or _iso(epoch), "type": kind, "payload": payload})


def _codex_setup(tmp_path):
    ctx, out = local_run(tmp_path)
    meta_path = Path(out["run_dir"]) / "context.json"
    meta_path.write_text(json.dumps({**json.loads(meta_path.read_text()), "runtime": "codex"}))
    ctx.env = {**ctx.env, "CODEX_HOME": str(tmp_path / "codex")}
    day = tmp_path / "codex" / "sessions" / "2026" / "11" / "19"
    day.mkdir(parents=True)
    return ctx, out, day, _started(ctx, out)


def test_handoff_copies_only_the_codex_review_turn(tmp_path):
    ctx, out, day, t0 = _codex_setup(tmp_path)
    run_id = out["run_id"]
    secret = "ghp_" + "Z9" * 20
    embedded = json.dumps({"password": "hunter2xyz", "note": "kept"})
    meta = {"id": "s", "creator_user_id": "user-SECRETID", "creator_account_id": "ACCOUNTID", "cwd": "/repo"}
    (day / "rollout-a.jsonl").write_text("\n".join([
        _codex_line("session_meta", t0 - 9000, meta),
        _codex_line("event_msg", t0 - 8000, {"type": "task_started"}),
        _codex_line("response_item", t0 - 7999, {"type": "message", "content": "UNRELATED PRIVATE TASK"}),
        _codex_line("event_msg", t0 - 5, {"type": "task_started"}),
        _codex_line("response_item", t0 + 1, {"type": "function_call_output",
                                              "output": f'{{"run_id": "{run_id}"}} {secret} {embedded}'}),
        _codex_line("response_item", t0 + 2, {"type": "reasoning", "encrypted_content": "BLOB"}),
        _codex_line("event_msg", t0 + 3, {"type": "token_count", "info": 1}),
        _codex_line("event_msg", t0 + 4, {"type": "task_complete", "last_agent_message": "REVIEW COMPLETE"}),
        _codex_line("event_msg", t0 + 5, {"type": "task_started", "turn_id": "LATER TURN"}),
        _codex_line("response_item", t0 + 6, {"type": "message", "content": "UNRELATED FOLLOW-UP"}),
    ]) + "\n")
    # a later session that only quotes the run id must not be chosen even though it is newer
    (day / "rollout-b.jsonl").write_text(
        _codex_line("response_item", t0 + 500, {"type": "message", "content": f"see {run_id}"}) + "\n")
    code, res = call(ctx, "handoff", "--run", run_id)
    assert code == 0 and res["transcript"] is True
    folder, data = handoff_files(ctx, run_id)
    assert data["transcript_files"] == ["rollout-a.jsonl"] and data["signals"] is None
    copied = (folder / "transcript" / "rollout-a.jsonl").read_text()
    entries = [json.loads(line) for line in copied.splitlines()]  # every kept line is still valid JSON
    assert run_id in copied and secret not in copied and "hunter2xyz" not in copied and "kept" in copied
    assert "UNRELATED PRIVATE TASK" not in copied
    assert "REVIEW COMPLETE" in copied
    assert "LATER TURN" not in copied and "UNRELATED FOLLOW-UP" not in copied
    assert "SECRETID" not in copied and "ACCOUNTID" not in copied and "BLOB" not in copied and "token_count" not in copied
    assert entries[0]["type"] == "session_meta"


def test_codex_session_choice_prefers_the_context_result_and_skips_unreadable_entries(tmp_path):
    ctx, out, day, t0 = _codex_setup(tmp_path)
    run_id = out["run_id"]
    quote = _codex_line("response_item", t0 - 100, {"type": "message", "content": f"see {run_id}"})
    (day / "rollout-quote.jsonl").write_text(quote + "\n")  # earlier, but only an incidental mention
    (day / "rollout-partial.jsonl").write_text(f'{{"timestamp": "2026-11-19T00:00:00Z", "run {run_id}\n')  # cut off mid-write
    (day / "rollout-naive.jsonl").write_text(
        _codex_line("response_item", 0, {"type": "message", "content": run_id}, stamp="2026-11-19T00:00:00") + "\n")
    (day / "rollout-ran.jsonl").write_text(
        _codex_line("response_item", t0 + 1, {"type": "function_call_output", "output": f'{{"run_id": "{run_id}"}}'}) + "\n")
    code, res = call(ctx, "handoff", "--run", run_id)
    assert code == 0 and res["transcript"] is True
    _, data = handoff_files(ctx, run_id)
    assert data["transcript_files"] == ["rollout-ran.jsonl"]


def test_handoff_accounting_survives_malformed_reviewer_lists_and_counts_a_name_once(tmp_path):
    ctx, out = local_run(tmp_path)
    run_dir = Path(out["run_dir"])
    path = run_dir / "findings" / "aggregator.json"
    agg = json.loads(path.read_text())
    agg["dropped"] = [{"reviewers": [1, {}, "skeptic", "skeptic"], "file": "a.py", "line": 1, "message": "m", "reason": "duplicate"},
                      {"reviewers": "skeptic"}, "junk"]
    path.write_text(json.dumps(agg))
    (run_dir / "findings" / "skeptic.json").write_text(json.dumps([FINDING, FAR, FAR]))
    code, _ = call(ctx, "handoff", "--run", out["run_id"])
    assert code == 0
    _, data = handoff_files(ctx, out["run_id"])
    assert data["dropped"] == agg["dropped"]  # the aggregator's own entries are kept as written
    # skeptic has 3 findings; it is named once on the kept item and once (not twice) on the first dropped item
    assert data["accounting"]["possibly_unaccounted"] == {"skeptic": 1}


def test_handoff_codex_run_without_a_session_says_so(tmp_path):
    ctx, out = local_run(tmp_path)
    meta_path = Path(out["run_dir"]) / "context.json"
    meta_path.write_text(json.dumps({**json.loads(meta_path.read_text()), "runtime": "codex"}))
    ctx.env = {**ctx.env, "CODEX_HOME": str(tmp_path / "empty")}
    call(ctx, "handoff", "--run", out["run_id"])
    _, data = handoff_files(ctx, out["run_id"])
    assert data["transcript"] is False and "Codex session" in data["transcript_reason"]


def test_markdown_shows_the_text_of_a_missed_thread():
    from codecheck.shadow.handoff import render_markdown
    cmp = {"human_comments": [{"thread_id": "T9", "path": "a.py", "line": 3, "author": "bob", "matched_finding": None,
                               "human_severity": None, "finding_severity": None, "severity_agrees": None,
                               "body": "this leaks the handle\non the error path"}],
           "unraised_findings": [], "misses": ["T9"], "general_comments": []}
    text = render_markdown({"run_id": "r", "skill": "review-pr", "mode": "pr", "comparison": cmp, "reviewers": {}, "findings": []})
    assert "T9 by bob at a.py:3: missed by us: this leaks the handle on the error path" in text


def test_comparison_body_is_collapsed_and_capped_like_general_comments():
    from codecheck.shadow.handoff import build_comparison
    rec = {"type": "human_comment", "thread_id": "T", "path": "a.py", "line": 1, "author": "bob",
           "body": "x  y\n" + "z" * 400, "matches_finding": None}
    (c,) = build_comparison([rec], [])["human_comments"]
    assert c["body"].startswith("x y z") and len(c["body"]) == 300


def test_handoff_findings_carry_their_message(tmp_path):
    long = {**FINDING, "message": "y is\nunused " + "z" * 400}
    _, clone = make_origin_and_clone(tmp_path)
    ctx = make_ctx(tmp_path, clone)
    enable_handoff(ctx, tmp_path)
    sh(clone, "checkout", "-b", "feat")
    commit_file(clone, "app.py", "x = 1\ny = 2\n", "add app")
    _code, out = call(ctx, "context", "--mode", "local", "--skill", "review-local")
    finish_review(ctx, out, {"skeptic": [long]}, {"findings": [{**long, "reviewers": ["skeptic"]}], "dropped": []})
    call(ctx, "ingest", "--run", out["run_id"])
    call(ctx, "post", "--run", out["run_id"])
    call(ctx, "handoff", "--run", out["run_id"])
    folder, data = handoff_files(ctx, out["run_id"])
    (finding,) = data["findings"]
    assert finding["message"].startswith("y is unused zzz") and len(finding["message"]) == 300
    assert "y is unused zzz" in (folder / "handoff.md").read_text()


def _findings_md(finding):
    from codecheck.shadow.handoff import render_markdown
    text = render_markdown({"run_id": "r", "skill": "review-pr", "mode": "pr", "comparison": None, "reviewers": {}, "findings": [finding]})
    return text.split("## Findings\n\n")[1].split("\n")[0]


def test_markdown_shows_a_finding_message_collapsed_and_capped():
    f = {"id": "abc", "file": "a.py", "line": 3, "severity": "warning", "category": "bug", "message": "leaks\nthe handle " + "q" * 300}
    assert _findings_md(f) == "- warning bug at a.py:3 (abc): leaks the handle " + "q" * (160 - len("leaks the handle "))


def test_markdown_finding_without_a_message_has_no_suffix():
    f = {"id": "abc", "file": "a.py", "line": 3, "severity": "warning", "category": "bug"}
    assert _findings_md(f) == "- warning bug at a.py:3 (abc)"
    assert _findings_md({**f, "message": "  "}) == "- warning bug at a.py:3 (abc)"


def test_comparison_keeps_threads_on_unreviewed_paths_out_of_misses():
    from codecheck.shadow.handoff import build_comparison
    recs = [{"type": "human_comment", "thread_id": t, "path": p, "line": 5, "author": "bob", "body": "hm", "matches_finding": None}
            for t, p in (("IN", "src/a.rs"), ("OUT", "tests/old.rs"))]
    cmp = build_comparison(recs, [], reviewed={"src/a.rs"})
    by_id = {c["thread_id"]: c for c in cmp["human_comments"]}
    assert by_id["IN"]["in_review"] is True and by_id["OUT"]["in_review"] is False
    assert cmp["misses"] == ["IN"]
    assert "in_review" not in build_comparison(recs, [])["human_comments"][0]  # no reviewed set, no claim
    assert build_comparison(recs, [])["misses"] == ["IN", "OUT"]


def test_markdown_says_a_thread_was_outside_the_reviewed_diff():
    from codecheck.shadow.handoff import render_markdown
    cmp = {"human_comments": [{"thread_id": "T9", "path": "old.rs", "line": 3, "author": "bob", "matched_finding": None,
                               "human_severity": None, "finding_severity": None, "severity_agrees": None,
                               "body": "move this", "in_review": False}],
           "unraised_findings": [], "misses": [], "general_comments": []}
    text = render_markdown({"run_id": "r", "skill": "review-pr", "mode": "pr", "comparison": cmp, "reviewers": {}, "findings": []})
    assert "T9 by bob at old.rs:3: not in the reviewed diff: move this" in text
    assert "missed by us" not in text


def test_reviewed_paths_come_from_the_run_chunks():
    from codecheck.commands.handoff import _reviewed_paths
    assert _reviewed_paths({"chunks": [["a.rs", "b.rs"], ["c.rs"]]}) == {"a.rs", "b.rs", "c.rs"}
    assert _reviewed_paths({}) is None
    assert _reviewed_paths({"chunks": []}) is None


def test_comparison_treats_a_thread_with_no_path_as_reviewed():
    from codecheck.shadow.handoff import build_comparison
    rec = {"type": "human_comment", "thread_id": "T", "path": None, "line": 1, "author": "bob", "body": "hm", "matches_finding": None}
    cmp = build_comparison([rec], [], reviewed={"a.py"})
    assert cmp["human_comments"][0]["in_review"] is True and cmp["misses"] == ["T"]


def test_handoff_posted_run_keeps_unreviewed_threads_out_of_misses(tmp_path):
    ctx, provider, pr, out = _pr_handoff_run(tmp_path)
    provider.threads[pr.ref] = [human("T2", 300, "rename this"), human("T3", 5, "moved away", path="gone.py")]
    call(ctx, "handoff", "--run", out["run_id"])
    folder, data = handoff_files(ctx, out["run_id"])
    cmp = data["comparison"]
    assert cmp["misses"] == ["T2"]
    assert {h["thread_id"]: h["in_review"] for h in cmp["human_comments"]} == {"T2": True, "T3": False}
    assert "T3 by bob at gone.py:5: not in the reviewed diff: moved away" in (folder / "handoff.md").read_text()


def test_handoff_unposted_run_keeps_unreviewed_threads_out_of_misses(tmp_path):
    ctx, provider, pr, out = _unposted_run(tmp_path)
    provider.threads[pr.ref] = [human("T2", 300, "rename this"), human("T3", 5, "moved away", path="gone.py")]
    call(ctx, "handoff", "--run", out["run_id"])
    _folder, data = handoff_files(ctx, out["run_id"])
    assert data["comparison"]["misses"] == ["T2"]
    assert {h["thread_id"]: h["in_review"] for h in data["comparison"]["human_comments"]} == {"T2": True, "T3": False}


def test_cleanup_writes_the_handoff_a_skipped_step_left_out(tmp_path):
    ctx, out = local_run(tmp_path)  # post ran, the skill's handoff step did not
    code, res = call(ctx, "cleanup", "--run", out["run_id"])
    assert code == 0 and res["handoff"] is True and res["run_dir_removed"] is True
    _folder, data = handoff_files(ctx, out["run_id"])
    assert data["run_id"] == out["run_id"] and len(data["findings"]) == 1
    assert ctx.shadow.read_raw(out["run_id"]) is not None


def test_cleanup_leaves_an_existing_handoff_alone(tmp_path):
    ctx, out = local_run(tmp_path)
    call(ctx, "handoff", "--run", out["run_id"])
    path = ctx.shadow.root / f"{out['run_id']}.handoff" / "handoff.json"
    before = path.read_text()
    ctx.provider = NoCalls()
    code, res = call(ctx, "cleanup", "--run", out["run_id"])
    assert code == 0 and "handoff" not in res and res["run_dir_removed"] is True
    assert path.read_text() == before


def test_cleanup_writes_no_handoff_when_the_flag_is_off(tmp_path):
    ctx, out = local_run(tmp_path, handoff=False)
    before = sorted(p.name for p in ctx.shadow.root.iterdir())
    code, res = call(ctx, "cleanup", "--run", out["run_id"])
    assert code == 0 and "handoff" not in res and res["run_dir_removed"] is True
    assert sorted(p.name for p in ctx.shadow.root.iterdir()) == before


def test_cleanup_still_cleans_up_when_the_handoff_fails(tmp_path, monkeypatch):
    import codecheck.commands.handoff as cmd

    ctx, out = local_run(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("broke")

    monkeypatch.setattr(cmd, "build_handoff", boom)
    code, res = call(ctx, "cleanup", "--run", out["run_id"])
    assert code == 0 and res["handoff"] is False and res["run_dir_removed"] is True
    assert "broke" in res["handoff_error"]
    assert not (ctx.shadow.root / f"{out['run_id']}.handoff" / "handoff.json").exists()


def test_cleanup_writes_no_handoff_when_the_flag_is_turned_off_after_context(tmp_path):
    ctx, out = local_run(tmp_path)  # context ran with the flag on, so the run asks for a hand-off
    ctx.config = replace(ctx.config, shadow=replace(ctx.config.shadow, handoff=False))
    code, res = call(ctx, "cleanup", "--run", out["run_id"])
    assert code == 0 and "handoff" not in res and res["run_dir_removed"] is True
    assert not (ctx.shadow.root / f"{out['run_id']}.handoff").exists()
