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
    assert "\n## Session\n" not in md.split("## Human comments", 1)[1].split("## Session", 1)[0] + "\n"


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


def test_handoff_skips_comparison_when_the_head_moved(tmp_path):
    ctx, provider, pr = pr_setup(tmp_path)
    enable_handoff(ctx, tmp_path)
    out = run_pr_context(ctx, pr)
    finish_review(ctx, out)
    call(ctx, "ingest", "--run", out["run_id"])
    provider.prs[pr.ref] = replace(pr, head_sha="f" * 40)
    _code, posted = call(ctx, "post", "--run", out["run_id"])
    assert posted["stale"] is True
    code, res = call(ctx, "handoff", "--run", out["run_id"])
    assert code == 0 and res["handoff"] is True and "not posted" in res["comparison"]


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
