import os
import stat
from datetime import UTC, datetime, timedelta

import pytest

from codecheck.core.feedback.draft import read_draft, render, write_draft
from codecheck.core.feedback.manifest import Manifest
from codecheck.core.feedback.signals import make_signal
from codecheck.core.feedback.store import FeedbackStore
from codecheck.core.state import StateStore
from codecheck.errors import ConfigError

T0 = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
M = Manifest(product="code-check", version="1.6.0", target_repo="subhiandrews/codecheck",
             kinds={"attempt-exhausted": "CI fix attempts ran out", "user-reported": "Reported by the user"})


def store(tmp_path, now=T0):
    box = {"now": now}
    s = FeedbackStore(StateStore(tmp_path / "state.json"), now=lambda: box["now"])
    return s, box


# signals

def test_fingerprint_is_stable_and_ignores_id_order():
    a = make_signal("attempt-exhausted", "babysit-pr", {"pr": 5, "check": "ci"})
    b = make_signal("attempt-exhausted", "babysit-pr", {"check": "ci", "pr": 5})
    assert a.fingerprint == b.fingerprint and len(a.fingerprint) == 12
    assert make_signal("attempt-exhausted", "babysit-pr", {"pr": 6, "check": "ci"}).fingerprint != a.fingerprint


def test_user_reports_with_different_notes_get_different_fingerprints():
    a = make_signal("user-reported", "feedback", {"note": "babysit reran wrongly"})
    b = make_signal("user-reported", "feedback", {"note": "anchoring is off"})
    assert a.fingerprint != b.fingerprint
    assert a.fingerprint == make_signal("user-reported", "feedback", {"note": "babysit reran wrongly"}).fingerprint


@pytest.mark.parametrize("ids,source", [({"bad key": "x"}, "ok"), ({"k\nx": "x"}, "ok"), ({"": "x"}, "ok"), ({"k": "x"}, "bad\nsource")])
def test_keys_and_source_must_be_plain_labels(ids, source):
    with pytest.raises(ValueError):
        make_signal("attempt-exhausted", source, ids)


@pytest.mark.parametrize("value", ["owner/repo#5", "has spaces in it", "x" * 121, "-leading", "line\nbreak"])
def test_id_values_must_be_short_labels_except_the_users_own_note(value):
    with pytest.raises(ValueError):
        make_signal("attempt-exhausted", "x", {"step": value})
    assert make_signal("user-reported", "feedback", {"note": value}).detail["note"]  # the user's own words are theirs to write


def test_signal_detail_is_scalar_capped_and_scrubbed():
    s = make_signal("user-reported", "feedback", {"note": "token=abcdef1234567890abcdef " + "x" * 900})
    note = s.detail["note"]
    assert "abcdef1234567890abcdef" not in note and len(note) <= 500
    with pytest.raises(TypeError):
        make_signal("user-reported", "feedback", {"note": ["list"]})


# manifest

def test_manifest_rejects_option_like_repo_labels_and_unknown_kind():
    for bad in ("-x/y", "nouser", "a/b/c", "a/--b"):
        with pytest.raises(ConfigError):
            Manifest(product="p", version="1", target_repo=bad, kinds={})
    assert M.title_for("attempt-exhausted") == "CI fix attempts ran out"
    with pytest.raises(ConfigError):
        M.title_for("nope")


# store: cooldown

def test_ask_rules_nothing_pending_then_pending_then_asked_today(tmp_path):
    s, box = store(tmp_path)
    assert s.decide(24) == (False, "nothing-pending")
    s.add(make_signal("attempt-exhausted", "babysit-pr", {"pr": 1}))
    assert s.decide(24) == (True, "ok")
    s.mark_asked()
    assert s.decide(24) == (False, "asked-recently")
    box["now"] = T0 + timedelta(hours=23)
    assert s.decide(24) == (False, "asked-recently")
    box["now"] = T0 + timedelta(hours=25)
    assert s.decide(24) == (True, "ok")


def test_decline_counts_as_asked_and_survives_a_new_store_instance(tmp_path):
    s, _ = store(tmp_path)
    s.add(make_signal("attempt-exhausted", "x", {"pr": 1}))
    s.mark_asked()
    s2, _ = store(tmp_path)
    assert s2.decide(24) == (False, "asked-recently")


def test_snooze_and_never(tmp_path):
    s, box = store(tmp_path)
    s.add(make_signal("attempt-exhausted", "x", {"pr": 1}))
    s.snooze(days=7)
    box["now"] = T0 + timedelta(days=3)
    assert s.decide(24) == (False, "snoozed")
    box["now"] = T0 + timedelta(days=8)
    assert s.decide(24) == (True, "ok")
    s.set_never()
    assert s.decide(24) == (False, "never")
    assert s.pending() == []  # never clears what was queued
    assert s.add(make_signal("attempt-exhausted", "x", {"pr": 2})) is None  # and stops collecting automatic signals


def test_user_reported_signals_are_collected_even_after_never(tmp_path):
    s, _ = store(tmp_path)
    s.set_never()
    assert s.add(make_signal("user-reported", "feedback", {"note": "hi"})) is not None


def test_same_fingerprint_counts_up_and_pending_is_ranked(tmp_path):
    s, _ = store(tmp_path)
    judged = make_signal("attempt-exhausted", "x", {"pr": 9}, judged=True)
    det = make_signal("attempt-exhausted", "x", {"pr": 1})
    s.add(judged)
    s.add(det)
    s.add(det)
    ranked = s.pending()
    assert [p["fingerprint"] for p in ranked] == [det.fingerprint, judged.fingerprint]
    assert ranked[0]["count"] == 2


def test_equal_counts_list_the_newest_first(tmp_path):
    s, box = store(tmp_path)
    old = make_signal("attempt-exhausted", "x", {"pr": 1})
    new = make_signal("attempt-exhausted", "x", {"pr": 2})
    s.add(old)
    box["now"] = T0 + timedelta(hours=1)
    s.add(new)
    assert [p["fingerprint"] for p in s.pending()] == [new.fingerprint, old.fingerprint]


def test_remove_pending(tmp_path):
    s, _ = store(tmp_path)
    sig = make_signal("attempt-exhausted", "x", {"pr": 1})
    s.add(sig)
    s.remove(sig.fingerprint)
    assert s.pending() == []


# draft

def test_render_has_marker_version_and_detail():
    sig = make_signal("attempt-exhausted", "babysit-pr", {"pr": 5, "check": "ci"})
    title, body = render(M, sig.kind, sig.source, sig.fingerprint, sig.detail, judged=False)
    assert title.startswith("[code-check] CI fix attempts ran out")
    assert f"<!-- feedback-fingerprint: {sig.fingerprint} -->" in body
    assert "code-check 1.6.0" in body and "check" in body
    assert "seen" not in body.lower()  # a count in the file would go stale once the draft exists


def test_render_scrubs_and_caps_body():
    _title, body = render(M, "user-reported", "feedback", "abc123def456", {"note": "ghp_" + "a" * 30, "extra": "y" * 20000}, judged=False)
    assert "ghp_" not in body and len(body) <= M.max_body_chars
    assert "feedback-fingerprint" in body  # the dedupe marker survives the cap


def test_write_and_read_draft_roundtrip_with_private_mode(tmp_path):
    path = write_draft(tmp_path / "fb", "abc123", "[code-check] t", "line1\nline2")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert read_draft(path) == ("[code-check] t", "line1\nline2")
    path.write_text("# edited title\n\nedited body\n")
    assert read_draft(path) == ("edited title", "edited body")
