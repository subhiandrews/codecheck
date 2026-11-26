from __future__ import annotations

import json

from dropped_raised import _handoffs, ledger, main, summary

DROPPED = {"file": "a.go", "line": 146, "reason": "out-of-scope", "reviewers": ["lang-go"],
           "message": "Compat subscriptions are owned by the root injector and are not released"}
KEPT = {"id": "f1", "file": "a.go", "line": 9, "message": "unrelated words entirely here"}


def _handoff(run_id, comments, dropped=(DROPPED,), findings=(KEPT,)):
    return {"run_id": run_id, "pr": "o/r#1", "findings": list(findings), "dropped": list(dropped),
            "comparison": {"human_comments": comments, "unraised_findings": [], "misses": [], "general_comments": []}}


def _comment(body, **kw):
    return {"thread_id": "T", "path": "a.go", "line": 300, "author": "h", "body": body, "matched_finding": None, **kw}


def test_a_dropped_finding_a_person_also_raised_is_listed_with_its_reason():
    h = _handoff("r1", [_comment("Note the existing limitation: compat subscriptions are owned by the root injector")])
    result = ledger([h])
    assert result["dropped_by_reason"] == {"out-of-scope": 1}
    assert result["dropped_with_related_comment"] == {"out-of-scope": 1}
    (hit,) = result["hits"]
    assert (hit["reason"], hit["reviewers"], hit["file"], hit["line"], hit["comment_author"]) == ("out-of-scope", ["lang-go"], "a.go", 146, "h")


def test_comments_matched_by_location_or_about_other_things_do_not_count():
    comments = [_comment("compat subscriptions are owned by the root injector", matched_finding="f1"),
                _comment("Please rename this variable"),
                _comment("compat subscriptions are owned by the root injector", path="b.go")]
    result = ledger([_handoff("r1", comments)])
    assert result["dropped_by_reason"] == {"out-of-scope": 1} and result["dropped_with_related_comment"] == {} and result["hits"] == []


def test_a_kept_finding_the_person_also_raised_is_not_a_dropped_hit():
    kept = {"id": "f1", "file": "a.go", "line": 9, "message": "compat subscriptions owned by the root injector"}
    result = ledger([_handoff("r1", [_comment("compat subscriptions are owned by the root injector")], dropped=[], findings=[kept])])
    assert result["hits"] == []


def test_one_dropped_finding_raised_by_two_comments_is_counted_once():
    both = [_comment("compat subscriptions owned by the root injector", thread_id="T1"),
            _comment("the compat subscriptions are owned by the root injector", thread_id="T2")]
    result = ledger([_handoff("r1", both)])
    assert result["dropped_by_reason"] == {"out-of-scope": 1} and result["dropped_with_related_comment"] == {"out-of-scope": 1}
    assert len(result["hits"]) == 2  # both comments are listed, the drop is counted once


def test_malformed_dropped_entries_are_skipped_not_fatal():
    messy = [DROPPED, "not a finding", None, {"file": "a.go", "line": 1, "reason": "x", "message": 5}]
    result = ledger([_handoff("r1", [_comment("compat subscriptions owned by the root injector")], dropped=messy)])
    assert result["dropped_by_reason"] == {"out-of-scope": 1, "x": 1} and [h["reason"] for h in result["hits"]] == ["out-of-scope"]


def test_a_stored_related_list_is_used_as_written():
    stored = {"kind": "dropped", "id": None, "reason": "low-value", "file": "a.go", "line": 146, "shared": ["x"]}
    result = ledger([_handoff("r1", [_comment("nothing in common", related=[stored])])])
    assert [h["reason"] for h in result["hits"]] == ["low-value"]


def test_summary_and_main_read_the_store(tmp_path, capsys):
    folder = tmp_path / "r1.handoff"
    folder.mkdir()
    (folder / "handoff.json").write_text(json.dumps(_handoff("r1", [_comment("compat subscriptions owned by the root injector")])))
    bad = tmp_path / "r2.handoff"
    bad.mkdir()
    (bad / "handoff.json").write_text("{not json")
    assert len(_handoffs(tmp_path)) == 1
    out = tmp_path / "ledger.json"
    assert main(["--store", str(tmp_path), "--json", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "1 hand-offs" in printed and "out-of-scope: 1 / 1" in printed
    assert json.loads(out.read_text())["dropped_with_related_comment"] == {"out-of-scope": 1}
    assert summary(ledger([]))[0].startswith("0 hand-offs")
