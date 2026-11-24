from __future__ import annotations

from handoff_pairs import build_pairs


def _human(thread: str, body: str = "why not expect()?"):
    return {"thread_id": thread, "path": "a.rs", "line": 5, "author": "maintainer", "body": body}


def _finding(file: str = "a.rs", message: str = "full message, not cut at 300"):
    return {"file": file, "line": 7, "message": message}


def test_pairs_every_human_with_every_kept_and_dropped_finding():
    handoff = {"pr": "o/r#1", "comparison": {"human_comments": [_human("PRRT_aaaaaa"), _human("PRRT_bbbbbb")]}}
    raw = {"aggregator": {"findings": [_finding()], "dropped": [_finding("b.rs"), _finding("c.rs")]}}
    pairs = build_pairs("run1", handoff, raw)
    assert len(pairs) == 2 * 3
    assert len({p["id"] for p in pairs}) == 6
    assert {p["candidate_stage"] for p in pairs} == {"kept", "dropped"}


def test_text_comes_from_the_raw_output():
    handoff = {"pr": "o/r#1", "comparison": {"human_comments": [_human("PRRT_aaaaaa")]}}
    raw = {"aggregator": {"findings": [_finding(message="x" * 500)], "dropped": []}}
    assert len(build_pairs("run1", handoff, raw)[0]["candidate_text"]) == 500


def test_no_pairs_without_candidates_or_comparison():
    handoff = {"pr": "o/r#1", "comparison": {"human_comments": [_human("PRRT_aaaaaa")]}}
    assert build_pairs("run1", handoff, {"aggregator": {"findings": [], "dropped": []}}) == []
    assert build_pairs("run1", {"pr": "o/r#1", "comparison": None}, {"aggregator": None}) == []
