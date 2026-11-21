from __future__ import annotations

import json

from scorecard import (
    build_dataset,
    by_version,
    golden_cases,
    score,
    store_cases,
    summarize,
)

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


def _write_store_run(directory, run_id: str, *, lines: list[int], kept: list[str], outputs: dict[str, bool], diff: bool = True):
    findings = [{"file": "a.py", "line": n, "message": "m"} for n in lines]
    (directory / f"{run_id}.raw.json").write_text(json.dumps({
        "reviewers": {"skeptic": findings},
        "aggregator": {"findings": [{"message": m, "reviewers": ["skeptic"]} for m in kept], "dropped": []},
        "root": None,
    }))
    if diff:
        (directory / f"{run_id}.diff").write_text(DIFF)
    folder = directory / f"{run_id}.handoff"
    folder.mkdir()
    (folder / "handoff.json").write_text(json.dumps({"tool_version": "9.9.9", "reviewers": {n: {"output": o} for n, o in outputs.items()}}))


def test_store_cases_carry_the_run_structure_and_skip_runs_without_a_diff(tmp_path):
    _write_store_run(tmp_path, "r1", lines=[2], kept=["a bug"], outputs={"skeptic": True})
    _write_store_run(tmp_path, "r2", lines=[2], kept=[], outputs={"skeptic": True}, diff=False)
    cases = store_cases(tmp_path)
    assert [c.name for c in cases] == ["store:r1"]
    meta = cases[0].metadata
    assert meta["tool_version"] == "9.9.9" and meta["reviewer_output"] == {"skeptic": True}
    assert meta["kept"][0]["message"] == "a bug" and meta["reviewers"]["skeptic"][0]["line"] == 2


def test_one_bad_store_file_does_not_stop_the_scorecard(tmp_path, capsys):
    _write_store_run(tmp_path, "r1", lines=[2], kept=["a bug"], outputs={"skeptic": True})
    _write_store_run(tmp_path, "r3", lines=[2], kept=[], outputs={"skeptic": True})
    (tmp_path / "r3.handoff" / "handoff.json").write_text("{not json")  # unreadable hand-off: score the run without it
    (tmp_path / "r2.raw.json").write_text('{"reviewers": {"skeptic": [')  # truncated raw output
    (tmp_path / "r2.diff").write_text(DIFF)
    (tmp_path / "r4.raw.json").write_text("[]")  # a list where a dict belongs
    (tmp_path / "r4.diff").write_text(DIFF)
    cases = {c.name: c for c in store_cases(tmp_path)}
    assert sorted(cases) == ["store:r1", "store:r3"]
    assert cases["store:r3"].metadata["reviewer_output"] is None and cases["store:r3"].metadata["tool_version"] is None
    err = capsys.readouterr().err
    assert "r2.raw.json" in err and "r4.raw.json" in err and "handoff.json" in err
    assert len(score(build_dataset(list(cases.values())))) > 0


def test_golden_cases_skip_the_hand_built_seed_case(tmp_path):
    body = {"diff": DIFF, "reviewer_outputs": {"skeptic": [{"file": "a.py", "line": 2}]}, "aggregator": {"findings": [], "dropped": []}}
    (tmp_path / "real-case.json").write_text(json.dumps(body))
    (tmp_path / "seed-pipeline.json").write_text(json.dumps(body))
    assert [c.name for c in golden_cases(tmp_path)] == ["golden:real-case"]


def test_score_pools_shares_across_runs_and_counts_runs_at_target(tmp_path):
    _write_store_run(tmp_path, "r1", lines=[2, 3], kept=["The added comments are over the cap."], outputs={"skeptic": True})
    _write_store_run(tmp_path, "r2", lines=[2], kept=["A bug."], outputs={"skeptic": False})
    dataset = build_dataset(store_cases(tmp_path))
    assert len(dataset.test_cases) == 2
    summary = {s["metric"]: s for s in summarize(score(dataset))}
    anchoring = summary["Reviewer findings on an added line"]
    assert (anchoring["pooled"], anchoring["of"], anchoring["runs"]) == (2, 3, 2)
    assert anchoring["runs_at_target"] is None  # no target agreed
    cap = summary["Kept findings that are not comment-cap findings"]
    assert (cap["pooled"], cap["of"], cap["runs_at_target"], cap["runs_with_target"]) == (1, 2, 1, 2)
    written = summary["Reviewers that wrote a findings file"]
    assert (written["pooled"], written["of"], written["runs_at_target"]) == (1, 2, 1)


def test_by_version_groups_each_run_under_its_tool_version(tmp_path):
    _write_store_run(tmp_path, "r1", lines=[2, 3], kept=[], outputs={"skeptic": True})
    _write_store_run(tmp_path, "r2", lines=[2], kept=[], outputs={"skeptic": True})
    (tmp_path / "r2.handoff" / "handoff.json").write_text(json.dumps({"tool_version": "1.0.0", "reviewers": {"skeptic": {"output": True}}}))
    grouped = by_version(score(build_dataset(store_cases(tmp_path))))
    assert list(grouped) == ["1.0.0", "9.9.9"]
    share = {v: next(s for s in g if s["metric"] == "Reviewer findings on an added line") for v, g in grouped.items()}
    assert (share["1.0.0"]["pooled"], share["1.0.0"]["of"]) == (1, 1)
    assert (share["9.9.9"]["pooled"], share["9.9.9"]["of"]) == (1, 2)


def test_a_metric_that_does_not_apply_is_left_out(tmp_path):
    body = {"diff": DIFF, "reviewer_outputs": {"skeptic": [{"file": "a.py", "line": 2}]}, "aggregator": {"findings": [], "dropped": []}}
    (tmp_path / "case.json").write_text(json.dumps(body))
    names = {r["metric"] for r in score(build_dataset(golden_cases(tmp_path)))}
    assert names == {"Reviewer findings on an added line"}  # nothing kept, no hand-off, nothing dropped
