from __future__ import annotations

import json

from anchoring import GOLDEN, check_findings, classify, summarize

from codecheck.core.diffmap import parse_diff

DIFF = """diff --git a/a.py b/a.py
index 1111111..2222222 100644
--- a/a.py
+++ b/a.py
@@ -1,3 +1,4 @@
 keep1
+added2
 keep3
 keep4
"""


def _good_run(directory, run_id="good"):
    (directory / f"{run_id}.raw.json").write_text(json.dumps({"reviewers": {"skeptic": [{"file": "a.py", "line": 2}]}, "root": None}))
    (directory / f"{run_id}.diff").write_text(DIFF)


def test_store_runs_skip_unreadable_or_oddly_shaped_files_and_say_so(tmp_path, capsys):
    from anchoring import store_runs

    _good_run(tmp_path)
    (tmp_path / "cut.raw.json").write_text('{"reviewers": {"skeptic": [')  # killed mid-write
    (tmp_path / "cut.diff").write_text(DIFF)
    (tmp_path / "listy.raw.json").write_text("[]")
    (tmp_path / "listy.diff").write_text(DIFF)
    (tmp_path / "nodict.raw.json").write_text(json.dumps({"reviewers": ["not", "a", "dict"]}))
    (tmp_path / "nodict.diff").write_text(DIFF)
    assert [r[0] for r in store_runs(tmp_path)] == ["good"]
    err = capsys.readouterr().err
    assert "cut.raw.json" in err and "listy.raw.json" in err  # the unreadable ones are reported, not silently dropped


def test_golden_cases_skip_files_missing_a_diff_or_outputs(tmp_path, capsys):
    from anchoring import golden_cases

    ok = {"diff": DIFF, "reviewer_outputs": {"skeptic": []}}
    (tmp_path / "ok.json").write_text(json.dumps(ok))
    (tmp_path / "no-diff.json").write_text(json.dumps({"reviewer_outputs": {}}))
    (tmp_path / "no-outputs.json").write_text(json.dumps({"diff": DIFF}))
    (tmp_path / "broken.json").write_text("{")
    assert [c[0] for c in golden_cases(tmp_path)] == ["ok"]
    err = capsys.readouterr().err
    assert "no-diff.json" in err and "broken.json" in err


def test_classify_covers_each_outcome():
    files = parse_diff(DIFF)
    assert classify(files, "a.py", 2) == ("added", 0)
    assert classify(files, "a.py", 3) == ("context", 0)
    assert classify(files, "a.py", 7) == ("relocated", 3)
    assert classify(files, "a.py", 8) == ("unanchored", None)
    assert classify(files, "b.py", 1) == ("file_not_in_diff", None)


def test_check_findings_strips_the_worktree_root_and_flags_malformed_findings():
    reviewers = {
        "skeptic": [{"file": "/work/tree/a.py", "line": 2}, {"file": "a.py"}, {"file": "a.py", "line": True}],
        "empty": [],
    }
    rows = check_findings(DIFF, reviewers, root="/work/tree")
    assert [r["category"] for r in rows] == ["added", "malformed", "malformed"]


def test_summarize_counts_each_reviewer():
    rows = check_findings(DIFF, {"a": [{"file": "a.py", "line": 2}, {"file": "a.py", "line": 3}], "b": [{"file": "z.py", "line": 1}]})
    out = summarize(rows)
    assert out["total"] == 3 and out["share_added"] == round(1 / 3, 3)
    assert out["counts"]["context"] == 1 and out["counts"]["file_not_in_diff"] == 1
    assert out["by_reviewer"]["a"] == {"total": 2, "added": 1, "not_added": 1}


def test_golden_tokio_8757_cites_a_line_that_is_not_added():
    # notes.md F6: three of four reviewers cite uring.rs:58, which is not an added line; the aggregator moved it to :87
    case = json.loads((GOLDEN / "tokio-rs-tokio-8757-f90e2156.json").read_text())
    files = parse_diff(case["diff"])
    path = "tokio/src/runtime/io/driver/uring.rs"
    assert classify(files, path, 58)[0] != "added"
    assert classify(files, path, 87)[0] == "added"
