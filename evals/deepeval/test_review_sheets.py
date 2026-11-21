from __future__ import annotations

import json

from review_sheets import (
    build_html,
    diff_window,
    full_text_for_drop,
    load_items,
    pr_url,
    sample,
)

DIFF = """diff --git a/a.py b/a.py
index 1111111..2222222 100644
--- a/a.py
+++ b/a.py
@@ -1,4 +1,5 @@
 keep1
-gone2
+added2
+added3
 keep4
 keep5
"""


def _write_run(store, run_id, pr, kept, dropped, reviewers=None):
    (store / f"{run_id}.diff").write_text(DIFF)
    (store / f"{run_id}.raw.json").write_text(json.dumps({
        "aggregator": {"findings": kept, "dropped": dropped}, "reviewers": reviewers or {}, "root": "/work/wt",
    }))
    meta = {"type": "run", "run_id": run_id, "pr": pr, "repo": "o/r", "head_sha": "abc"}
    (store / f"{run_id}.jsonl").write_text(json.dumps(meta) + "\n")


def _kept(n):
    return {"file": "a.py", "line": 2, "message": f"kept {n}", "severity": "warning", "category": "c", "reviewers": ["x"]}


def _drop(n, reason="low-value"):
    return {"file": "a.py", "line": 2, "message": f"drop {n}", "reason": reason, "reviewers": ["x"]}


def test_load_items_skips_duplicates_and_tags_kind(tmp_path):
    _write_run(tmp_path, "r1", "o/r#1", [_kept(1)], [_drop(1), _drop(2, "duplicate")])
    items = load_items(tmp_path)
    assert sorted(i["kind"] for i in items) == ["drop", "kept"]
    assert all(i["pr"] == "o/r#1" for i in items)


def test_sample_is_seeded_and_caps_per_pr(tmp_path):
    for n in range(6):
        _write_run(tmp_path, f"r{n}", "o/r#1" if n < 4 else f"o/r#{n}", [], [_drop(n)])
    items = load_items(tmp_path)
    a = sample(items, "drop", 4, seed=1, per_pr=2)
    b = sample(items, "drop", 4, seed=1, per_pr=2)
    assert [i["id"] for i in a] == [i["id"] for i in b]
    assert len(a) == 4
    assert sum(i["pr"] == "o/r#1" for i in a) <= 2


def test_sample_filters_by_reason_and_skips_excluded_ids(tmp_path):
    _write_run(tmp_path, "r1", "o/r#1", [], [_drop(1, "not-verified"), _drop(2, "low-value"), _drop(3, "not-verified")])
    items = load_items(tmp_path)
    got = sample(items, "drop", 10, seed=1, per_pr=99, reason="not-verified")
    assert sorted(i["id"] for i in got) == ["r1:d0", "r1:d2"]
    got = sample(items, "drop", 10, seed=1, per_pr=99, reason="not-verified", exclude={"r1:d0"})
    assert [i["id"] for i in got] == ["r1:d2"]


def test_build_html_uses_its_own_storage_key(tmp_path):
    _write_run(tmp_path, "r1", "o/r#1", [], [_drop(1)])
    items = load_items(tmp_path)
    assert "KEY='review-sheet-not-verified'" in build_html(items, "drop", "T", key="review-sheet-not-verified")
    assert "KEY='review-sheet-drop'" in build_html(items, "drop", "T")


def test_sample_returns_fewer_when_pool_is_small(tmp_path):
    _write_run(tmp_path, "r1", "o/r#1", [_kept(1)], [])
    assert len(sample(load_items(tmp_path), "kept", 20, seed=1, per_pr=2)) == 1


def test_full_text_for_drop_uses_the_reviewers_own_words():
    reviewers = {"x": [{"file": "a.py", "line": 3, "message": "long original text"}]}
    drop = {"file": "a.py", "line": 2, "message": "short summary", "reviewers": ["x"]}
    assert full_text_for_drop(drop, reviewers, None) == "long original text"
    assert full_text_for_drop(drop, {}, None) == "short summary"


def test_diff_window_marks_the_cited_line_and_keeps_deletions():
    rows = diff_window(DIFF, "a.py", 3, radius=2)
    texts = [r["text"] for r in rows]
    assert "added3" in texts and "gone2" in texts
    assert [r["text"] for r in rows if r["cited"]] == ["added3"]


def test_diff_window_reports_a_line_outside_the_diff():
    assert diff_window(DIFF, "a.py", 900, radius=2) == []
    assert diff_window(DIFF, "missing.py", 1, radius=2) == []


def test_pr_url_by_host_and_none_for_local_runs():
    assert pr_url("tokio-rs/tokio", "tokio-rs/tokio#8804") == "https://github.com/tokio-rs/tokio/pull/8804"
    assert pr_url("gitlab-org/cli", "gitlab-org/cli#4455") == "https://gitlab.com/gitlab-org/cli/-/merge_requests/4455"
    assert pr_url("o/r", "0fe07d93479c") is None  # a local run has no PR
    assert pr_url(None, "o/r#1") is None


def test_build_html_links_the_pr(tmp_path):
    _write_run(tmp_path, "r1", "o/r#1", [_kept(1)], [])
    html = build_html(load_items(tmp_path), "kept", "Kept")
    assert '<a href="https://github.com/o/r/pull/1"' in html


def test_build_html_hides_the_aggregator_reason_until_reveal(tmp_path):
    _write_run(tmp_path, "r1", "o/r#1", [_kept(1)], [_drop(1)])
    items = load_items(tmp_path)
    html = build_html(items, "drop", "Drops")
    before_reveal = html.split("<details")[0]
    assert "low-value" not in before_reveal
    assert 'value="right"' in html and 'value="wrong"' in html
    kept_html = build_html(items, "kept", "Kept")
    assert 'value="worth"' in kept_html
