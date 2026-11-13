import json
import stat
from dataclasses import replace
from pathlib import Path

import pytest
from clihelp import call, finish_review, make_ctx, pr_setup, run_pr_context
from gitutil import commit_file, make_origin_and_clone, sh

from codecheck.cli import build_parser
from codecheck.models import Thread, ThreadComment
from codecheck.shadow.handoff import build_comparison, implied_severity, render_markdown

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
    main.write_text(json.dumps({"type": "user", "message": {"content": f"RUN_DIR=x/{out['run_id']} {secret}"}}) + "\n")
    sub = session / "s1" / "subagents"
    sub.mkdir(parents=True)
    tool = {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "a", "name": "Read", "input": {}}]}}
    err = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "a", "is_error": True, "content": "nope"}]}}
    (sub / "agent-1.jsonl").write_text(json.dumps(tool) + "\n" + json.dumps(err) + "\n")
    code, res = call(ctx, "handoff", "--run", out["run_id"])
    assert code == 0 and res["transcript"] is True
    folder, data = handoff_files(ctx, out["run_id"])
    assert data["transcript"] is True and data["transcript_files"] == ["s1.jsonl", "agent-1.jsonl"]
    assert data["signals"]["tool_calls"] == 1 and data["signals"]["tool_errors"][0]["tool"] == "Read"
    copied = folder / "transcript" / "s1.jsonl"
    assert secret not in copied.read_text() and out["run_id"] in copied.read_text()


def test_handoff_saves_raw_outputs(tmp_path):
    ctx, out = local_run(tmp_path)
    call(ctx, "handoff", "--run", out["run_id"])
    raw = ctx.shadow.read_raw(out["run_id"])
    assert raw["reviewers"]["skeptic"][0]["line"] == 2
    assert set(raw["reviewers"]) <= set(out["panel"]["members"])
    assert raw["aggregator"]["findings"][0]["message"] == "y is unused"
    assert raw["panel"] == out["panel"] and raw["chunks"] == out["chunks"] and raw["base_ref"]


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


def test_handoff_comparison_failure_still_writes_the_files(tmp_path):
    ctx, provider, _pr, out = _pr_handoff_run(tmp_path)

    def offline(ref, reactions=True):
        raise RuntimeError("gh: network down")

    provider.list_threads = offline
    code, res = call(ctx, "handoff", "--run", out["run_id"])
    assert code == 0 and res["handoff"] is True and "network down" in res["comparison"]
    _folder, data = handoff_files(ctx, out["run_id"])
    assert data["comparison"] is None and "network down" in data["comparison_reason"]
