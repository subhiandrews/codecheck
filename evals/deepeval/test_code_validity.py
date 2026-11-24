from __future__ import annotations

import json

import pytest
from code_validity import (
    BAR_CORRECT,
    BAR_TRUE_FOUND,
    EXCLUDED,
    bar,
    build_context,
    diff_sections,
    label_for,
    load_truth,
    numbered_window,
    safe_ref,
)

DIFF = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-old a
+new a
diff --git a/b.py b/b.py
--- a/b.py
+++ b/b.py
@@ -1 +1 @@
-old b
+new b
diff --git a/c.py b/c.py
--- a/c.py
+++ b/c.py
@@ -1 +1 @@
-old c
+new c
"""


@pytest.mark.parametrize(
    ("score", "label"),
    [(1.0, "TRUE"), (0.9, "TRUE"), (0.8, "UNCERTAIN"), (0.4, "UNCERTAIN"), (0.3, "FALSE"), (0.0, "FALSE")],
)
def test_label_for_cutoffs(score, label):
    assert label_for(score) == label


def _truth():
    return {f"t{i}": "TRUE" for i in range(4)} | {f"f{i}": "FALSE" for i in range(5)}


def test_bar_fails_an_always_false_judge():
    predicted = {k: "FALSE" for k in _truth()}
    result = bar(predicted, _truth())
    assert (result["correct"], result["of"], result["true_found"]) == (5, 9, 0)
    assert result["passes"] is False


def test_bar_fails_an_always_true_judge():
    result = bar({k: "TRUE" for k in _truth()}, _truth())
    assert (result["correct"], result["true_found"]) == (4, 4)
    assert result["passes"] is False


def test_bar_passes_at_eight_correct_with_three_true():
    predicted = dict(_truth())
    predicted["t3"] = "FALSE"  # one TRUE missed
    result = bar(predicted, _truth())
    assert (result["correct"], result["true_found"]) == (BAR_CORRECT, BAR_TRUE_FOUND)
    assert result["passes"] is True


def test_bar_fails_at_seven_correct():
    predicted = dict(_truth())
    predicted["t3"], predicted["f0"] = "FALSE", "TRUE"
    result = bar(predicted, _truth())
    assert result["correct"] == BAR_CORRECT - 1
    assert result["passes"] is False


def test_uncertain_and_missing_count_as_wrong():
    predicted = {k: "UNCERTAIN" for k in _truth()}
    assert bar(predicted, _truth())["correct"] == 0
    assert bar({}, _truth())["correct"] == 0


def test_numbered_window_marks_the_cited_line_and_clips():
    text = "\n".join(f"line{i}" for i in range(1, 11))
    window = numbered_window(text, 5, radius=2)
    assert window.splitlines() == ["   3| line3", "   4| line4", ">  5| line5", "   6| line6", "   7| line7"]
    assert numbered_window(text, 1, radius=2).splitlines()[0] == ">  1| line1"
    assert numbered_window(text, 99, radius=2).splitlines()[-1].endswith("line10")


def test_diff_sections_puts_the_cited_file_first_and_caps_the_rest():
    out = diff_sections(DIFF, "b.py", cap=10_000)
    assert out.index("a/b.py") < out.index("a/a.py") < out.index("a/c.py")
    capped = diff_sections(DIFF, "b.py", cap=len(DIFF) // 2)
    assert "a/b.py" in capped and "a/c.py" not in capped
    assert "not shown" in capped


def test_diff_sections_keeps_the_cited_file_even_if_it_alone_exceeds_the_cap():
    out = diff_sections(DIFF, "b.py", cap=5)
    assert "a/b.py" in out and "not shown" in out


def test_safe_ref_rejects_anything_that_could_act_as_an_option_or_escape():
    assert safe_ref("abcdef1234", "src/a.py") == "abcdef1234:src/a.py"
    for commit, path in [("--output=x", "a.py"), ("abcdef1", "-x"), ("abcdef1", "../a.py"), ("abcdef1", "/etc/passwd"), ("zzz", "a.py")]:
        with pytest.raises(ValueError, match="unsafe"):
            safe_ref(commit, path)


def test_build_context_returns_file_window_then_diff(tmp_path):
    diff_file = tmp_path / "x.diff"
    diff_file.write_text(DIFF)
    task = {"clone": "/c", "commit": "abcdef1", "file": "b.py", "line": 2, "diff_file": str(diff_file)}
    parts = build_context(task, git_show=lambda clone, ref: "one\ntwo\nthree\n")
    assert ">  2| two" in parts[0] and "b.py" in parts[0]
    assert "diff --git a/b.py" in parts[1]


def _task(tmp_path, line):
    diff_file = tmp_path / "x.diff"
    diff_file.write_text(DIFF)
    return {"clone": "/c", "commit": "abcdef1", "file": "b.py", "line": line, "diff_file": str(diff_file)}


def test_build_context_gives_the_whole_file_when_it_fits_the_cap(tmp_path):
    text = "\n".join(f"line{i}" for i in range(1, 501))
    parts = build_context(_task(tmp_path, 250), git_show=lambda clone, ref: text)
    assert "whole file" in parts[0]
    assert "   1| line1" in parts[0] and " 500| line500" in parts[0] and ">250| line250" in parts[0]


def test_build_context_uses_a_window_when_the_file_exceeds_the_cap(tmp_path):
    text = "\n".join(f"line{i}" for i in range(1, 2001))
    parts = build_context(_task(tmp_path, 1000), git_show=lambda clone, ref: text, file_cap=1000, file_radius=10)
    assert "whole file" not in parts[0] and "lines 990-1010 of 2000" in parts[0]
    assert "line1000" in parts[0] and "line500" not in parts[0]


def test_build_context_says_so_when_the_file_is_not_at_the_commit(tmp_path):
    diff_file = tmp_path / "x.diff"
    diff_file.write_text(DIFF)
    task = {"clone": "/c", "commit": "abcdef1", "file": "b.py", "line": 2, "diff_file": str(diff_file)}

    def missing(clone, ref):
        raise FileNotFoundError(ref)

    parts = build_context(task, git_show=missing)
    assert "does not exist at commit" in parts[0]


def test_load_truth_keeps_only_items_both_passes_call_true_or_false(tmp_path):
    path = tmp_path / "agreement.json"
    path.write_text(json.dumps({"agreed": {"a": "TRUE", "b": "FALSE", "c": "PARTLY", "d": "UNVERIFIABLE"}}))
    assert load_truth(path) == {"a": "TRUE", "b": "FALSE"}


def test_load_truth_leaves_out_the_items_whose_deciding_code_the_judge_cannot_see(tmp_path):
    path = tmp_path / "agreement.json"
    agreed = {k: "FALSE" for k in sorted(EXCLUDED)} | {"keep": "TRUE"}
    path.write_text(json.dumps({"agreed": agreed}))
    assert load_truth(path) == {"keep": "TRUE"}
    assert EXCLUDED == {"2448c039ebb0:d1", "84048732c6dd:d0", "84048732c6dd:d1"}
