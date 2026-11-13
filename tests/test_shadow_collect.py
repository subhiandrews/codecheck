import json
import os

import pytest

from clihelp import call, make_ctx
from codecheck.models import PRRef, Thread, ThreadComment
from codecheck.shadow.collect import collect_outcomes
from codecheck.shadow.export import export_golden
from codecheck.shadow.store import ShadowStore
from fakes import FakeProvider

RID = "abcdef012345"
REF = PRRef("o/r", 7)


def seed(shadow):
    shadow.append(RID, {"type": "run", "pr": "o/r#7", "repo": "o/r", "head_sha": "abc", "panel": [], "models": {}, "tool_version": "0"})
    shadow.save_diff(RID, "diff --git a/a.py b/a.py\n")
    for fid, line in (("aaaaaaaaaaaa", 3), ("bbbbbbbbbbbb", 8), ("cccccccccccc", 20), ("dddddddddddd", 40)):
        shadow.append(RID, {"type": "finding", "id": fid, "file": "a.py", "line": line, "severity": "warning",
                            "category": "bug", "message": f"m{fid[0]}", "reviewers": ["skeptic"], "inline": True})


def tc(cid, author, body, reactions=()):
    return ThreadComment(cid, author, body, "t", tuple(reactions))


def threads():
    marker = lambda fid: f"**Warning**\n<!-- code-check:finding:{fid} -->"  # noqa: E731
    return [
        Thread("T1", "a.py", 3, True, False, (tc(1, "me", marker("a" * 12), ["THUMBS_UP"]),)),
        Thread("T2", "a.py", 8, False, False, (tc(2, "me", marker("b" * 12)), tc(3, "alice", "this is not a bug"),
                                              tc(4, "me", "explained\n<!-- code-check:reply -->"))),
        Thread("T3", "a.py", 21, False, False, (tc(5, "alice", "please look at line 21"),)),
        Thread("T4", "b.py", 5, False, False, (tc(6, "alice", "you missed this"),)),
        Thread("T5", "a.py", 3, False, False, (tc(7, "ci[bot]", "coverage dropped"),)),
        Thread("T6", "a.py", 40, False, False, (tc(8, "me", marker("d" * 12)),)),
    ]


def test_collect_records_outcomes_and_human_comments(tmp_path):
    shadow = ShadowStore(tmp_path)
    seed(shadow)
    provider = FakeProvider()
    provider.threads[REF] = threads()
    summary = collect_outcomes(provider, shadow, REF, "me")
    assert summary == {"runs": 1, "outcomes": 3, "human_comments": 2}
    recs = shadow.read(RID)
    outcomes = {r["finding_id"]: r for r in recs if r["type"] == "outcome"}
    assert outcomes["a" * 12]["resolved"] is True and outcomes["a" * 12]["reactions"] == {"THUMBS_UP": 1}
    assert [r["author"] for r in outcomes["b" * 12]["replies"]] == ["alice"]  # our own reply is not feedback
    humans = {r["thread_id"]: r for r in recs if r["type"] == "human_comment"}
    assert humans["T3"]["matches_finding"] == "c" * 12 and humans["T4"]["matches_finding"] is None
    assert "T5" not in humans  # bots are not human feedback


def test_export_golden_labels_and_misses_and_is_idempotent(tmp_path):
    shadow = ShadowStore(tmp_path)
    seed(shadow)
    provider = FakeProvider()
    provider.threads[REF] = threads()
    collect_outcomes(provider, shadow, REF, "me")
    collect_outcomes(provider, shadow, REF, "me")  # collecting twice must not change the export
    out = tmp_path / "golden.jsonl"
    assert export_golden(shadow, out) == 1
    (row,) = [json.loads(line) for line in out.read_text().splitlines()]
    labels = {f["id"]: f["label"] for f in row["findings"]}
    assert labels == {"a" * 12: "confirmed", "b" * 12: "disputed", "c" * 12: "confirmed", "d" * 12: "no_signal"}
    assert [(m["path"], m["author"]) for m in row["misses"]] == [("b.py", "alice")]
    assert row["pr"] == "o/r#7" and row["label_status"] == "auto" and row["diff"].startswith("diff --git")


def test_runs_without_any_human_signal_are_not_exported(tmp_path):
    shadow = ShadowStore(tmp_path)
    seed(shadow)
    assert export_golden(shadow, tmp_path / "g.jsonl") == 0


def test_collect_with_no_runs_is_a_noop(tmp_path):
    assert collect_outcomes(FakeProvider(), ShadowStore(tmp_path), REF, "me") == {"runs": 0, "outcomes": 0, "human_comments": 0}


def test_cli_collect_export_and_record_verdict(tmp_path):
    ctx = make_ctx(tmp_path, tmp_path)
    seed(ctx.shadow)
    ctx.provider.threads[REF] = threads()
    code, out = call(ctx, "shadow", "collect", "--pr", "o/r#7")
    assert code == 0 and out["outcomes"] == 3
    code, out = call(ctx, "shadow", "collect", "--all")
    assert out["results"][0]["pr"] == "o/r#7"
    code, out = call(ctx, "shadow", "export-golden", "--out", str(tmp_path / "g.jsonl"))
    assert out["exported"] == 1

    verdicts = [{"thread_id": "T1", "verdict": "valid", "reasoning": "reproduced", "action": "fixed", "commit": "abc"}]
    code, out = call(ctx, "shadow", "record-verdict", "--run", RID, stdin=json.dumps(verdicts))
    assert out["recorded"] == 1
    assert any(r["type"] == "verdict" and r["thread_id"] == "T1" for r in ctx.shadow.read(RID))
    code, out = call(ctx, "shadow", "record-verdict", "--run", RID, stdin=json.dumps([{"thread_id": "T1", "verdict": "maybe"}]))
    assert code == 1 and "verdict" in out["error"]


def test_cli_requires_shadow_enabled(tmp_path):
    ctx = make_ctx(tmp_path, tmp_path)
    ctx.shadow = None
    code, out = call(ctx, "shadow", "export-golden", "--out", str(tmp_path / "g.jsonl"))
    assert code == 1 and "disabled" in out["error"]


def _setup(tmp_path, thr):
    shadow = ShadowStore(tmp_path)
    seed(shadow)
    provider = FakeProvider()
    provider.threads[REF] = thr
    return shadow, provider


def test_forged_marker_in_message_or_by_human_does_not_misattribute(tmp_path):
    marker = lambda fid: f"<!-- code-check:finding:{fid} -->"  # noqa: E731
    thr = [
        # our comment whose message embeds a forged marker for finding b; the real (trailing) marker is a
        Thread("T1", "a.py", 3, True, False, (tc(1, "me", f"**Warning**\n{marker('b' * 12)}\ntext\n{marker('a' * 12)}"),)),
        # a human pastes a marker for finding d
        Thread("T2", "a.py", 40, True, False, (tc(2, "alice", f"fake {marker('d' * 12)}"),)),
        # marker for a finding on a different file
        Thread("T3", "z.py", 8, True, False, (tc(3, "me", marker("b" * 12)),)),
    ]
    shadow, provider = _setup(tmp_path, thr)
    summary = collect_outcomes(provider, shadow, REF, "me")
    recs = shadow.read(RID)
    outcomes = [r for r in recs if r["type"] == "outcome"]
    assert [(o["finding_id"], o["thread_id"]) for o in outcomes] == [("a" * 12, "T1")]
    assert summary["outcomes"] == 1
    assert [r["thread_id"] for r in recs if r["type"] == "human_comment"] == ["T2"]


def test_export_is_private_and_scrubbed(tmp_path):
    shadow, provider = _setup(tmp_path / "s", threads())
    shadow.append(RID, {"type": "human_comment", "thread_id": "T9", "path": "q.py", "line": 1, "author": "bob",
                        "body": "token ghp_" + "a" * 36, "matches_finding": None})
    out = tmp_path / "new" / "g.jsonl"
    assert export_golden(shadow, out) == 1
    assert "ghp_" + "a" * 36 not in out.read_text()
    assert os.stat(out).st_mode & 0o777 == 0o600
    os.symlink(tmp_path / "elsewhere", tmp_path / "link.jsonl")
    with pytest.raises(OSError):
        export_golden(shadow, tmp_path / "link.jsonl")
    assert not (tmp_path / "elsewhere").exists()


def test_record_verdict_whitelists_fields_and_checks_run(tmp_path):
    ctx = make_ctx(tmp_path, tmp_path)
    seed(ctx.shadow)
    v = [{"thread_id": "T1", "verdict": "valid", "type": "run", "run_id": "x", "junk": 1, "reasoning": "r"}]
    code, out = call(ctx, "shadow", "record-verdict", "--run", RID, stdin=json.dumps(v))
    assert code == 0
    rec = [r for r in ctx.shadow.read(RID) if r.get("thread_id") == "T1" and r["type"] == "verdict"][0]
    assert rec["run_id"] == RID and "junk" not in rec
    code, out = call(ctx, "shadow", "record-verdict", "--run", "../x", stdin="[]")
    assert code == 1
    code, out = call(ctx, "shadow", "record-verdict", "--run", "111111111111", stdin=json.dumps(v))
    assert code == 1 and "unknown run" in out["error"]
    code, out = call(ctx, "shadow", "record-verdict", "--run", RID, stdin="{}")
    assert code == 1


def _two_runs(tmp_path):
    """Run ids chosen so lexical order is the OPPOSITE of chronological order."""
    old, new = "ffffffffffff", "000000000000"
    clock = iter(range(1000, 2000))
    shadow = ShadowStore(tmp_path, now=lambda: next(clock))
    for rid in (old, new):  # old is created first
        shadow.append(rid, {"type": "run", "pr": "o/r#7", "repo": "o/r", "head_sha": rid, "panel": [], "models": {}, "tool_version": "0"})
        shadow.save_diff(rid, "diff --git a/a.py b/a.py\n")
        shadow.append(rid, {"type": "finding", "id": "a" * 12, "file": "a.py", "line": 3, "severity": "warning",
                            "category": "bug", "message": "m", "reviewers": ["skeptic"], "inline": True})
    shadow.append(old, {"type": "finding", "id": "e" * 12, "file": "a.py", "line": 50, "severity": "warning",
                        "category": "bug", "message": "m", "reviewers": ["skeptic"], "inline": True})
    return shadow, old, new


def test_runs_are_chronological_not_lexical(tmp_path):
    shadow, old, new = _two_runs(tmp_path)
    assert shadow.runs_for_pr("o/r#7") == [old, new]


def test_multi_run_attribution_and_export_dedupe(tmp_path):
    shadow, old, new = _two_runs(tmp_path)
    marker = f"x\n<!-- code-check:finding:{'a' * 12} -->"
    thr = [
        Thread("T1", "a.py", 3, True, False, (tc(1, "me", marker),)),
        Thread("T2", "a.py", 4, False, False, (tc(2, "alice", "near a"),)),     # matches a in newest run
        Thread("T3", "a.py", 50, False, False, (tc(3, "alice", "near e"),)),    # only old run has e
        Thread("T4", "z.py", 1, False, False, (tc(4, "alice", "unmatched"),)),  # falls back to newest run
    ]
    provider = FakeProvider()
    provider.threads[REF] = thr
    collect_outcomes(provider, shadow, REF, "me")
    collect_outcomes(provider, shadow, REF, "me")
    where = {r["thread_id"]: rid for rid in (old, new) for r in shadow.read(rid) if r["type"] == "human_comment"}
    assert where == {"T2": new, "T3": old, "T4": new}
    assert [r["finding_id"] for r in shadow.read(new) if r["type"] == "outcome"] == ["a" * 12, "a" * 12]
    assert not [r for r in shadow.read(old) if r["type"] == "outcome"]
    out = tmp_path / "g.jsonl"
    assert export_golden(shadow, out) == 2
    rows = {r["run_id"]: r for r in map(json.loads, out.read_text().splitlines())}
    assert {f["id"]: f["label"] for f in rows[new]["findings"]} == {"a" * 12: "confirmed"}
    assert {f["id"]: f["label"] for f in rows[old]["findings"]} == {"a" * 12: "unreviewed", "e" * 12: "confirmed"}
    misses = [(m["path"]) for r in rows.values() for m in r["misses"]]
    assert misses == ["z.py"]


def test_export_tolerates_malformed_records(tmp_path):
    shadow, old, new = _two_runs(tmp_path)
    shadow.append(new, {"type": "finding", "file": "a.py"})  # no id
    shadow.append(new, {"type": "outcome", "thread_id": "T9"})  # no finding_id
    shadow.append(new, {"type": "human_comment", "thread_id": "T8", "path": "q", "author": "b", "body": "x"})
    assert export_golden(shadow, tmp_path / "g.jsonl") == 1
