import json
import os
import sqlite3
import stat
from dataclasses import replace

import pytest
from clihelp import call, make_ctx
from gitutil import make_origin_and_clone
from test_memory_measure import Clock, rid_of

from codecheck.config import MemoryConfig
from codecheck.memory import db as dbmod
from codecheck.memory.consolidate import DAY, Params, consolidate
from codecheck.memory.db import MemoryDB, MemoryStoreError
from codecheck.memory.evidence import MESSAGE_CAP, QUOTE_CAP, clip, load_runs
from codecheck.memory.model import Evidence, Scope, evidence_id, lesson_id
from codecheck.shadow.store import ShadowStore

T0 = 1_700_000_000.0
SCOPE = Scope("github.com", "o/r", "src/app/**", "python", "tests")


def ev(signal, pr, thread, ts=T0, role="maintainer", scope=SCOPE, quote=None, finding=None):
    kind = "miss" if signal == "human_miss" else "finding"
    return Evidence(evidence_id("github.com", scope.repo, pr, thread, signal), lesson_id(scope, kind), scope, signal, ts, pr,
                    thread, role, "1", quote, finding)


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def db(tmp_path):
    return MemoryDB(tmp_path / "mem", now=lambda: T0 + DAY)


def test_reading_a_missing_store_is_empty_and_creates_nothing(tmp_path):
    d = MemoryDB(tmp_path / "nope")
    assert d.lessons() == [] and d.evidence() == [] and d.lesson("x") is None
    assert d.status() == {"exists": False, "evidence": 0, "lessons": {}, "schema": 0}
    assert not (tmp_path / "nope").exists()


def test_the_file_is_0600_in_a_0700_directory_with_wal_and_a_schema_version(db):
    db.upsert_evidence([ev("verdict_valid", "o/r#1", "A")])
    assert mode(db.directory) == 0o700 and mode(db.file) == 0o600
    conn = sqlite3.connect(db.file)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == dbmod.SCHEMA_VERSION
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()
    for side in db.directory.glob("memory.db-*"):
        assert mode(side) == 0o600


def test_a_loose_existing_directory_is_tightened(tmp_path):
    d = tmp_path / "mem"
    d.mkdir(mode=0o755)
    MemoryDB(d).upsert_evidence([ev("verdict_valid", "o/r#1", "A")])
    assert mode(d) == 0o700


def test_a_symlinked_store_file_is_refused(tmp_path):
    d = tmp_path / "mem"
    d.mkdir()
    (d / "memory.db").symlink_to(tmp_path / "elsewhere")
    assert not (d / "memory.db").is_file()  # dangling link: treated as absent, creation refuses to follow it
    with pytest.raises(MemoryStoreError):
        MemoryDB(d).upsert_evidence([ev("verdict_valid", "o/r#1", "A")])
    assert not (tmp_path / "elsewhere").exists()


def test_upsert_is_idempotent_on_the_evidence_id(db):
    items = [ev("verdict_valid", "o/r#1", "A"), ev("verdict_valid", "o/r#2", "B")]
    assert dict(db.upsert_evidence(items)) == {"new": 2}
    assert dict(db.upsert_evidence(items)) == {"unchanged": 2}
    assert len(db.evidence()) == 2


def test_a_newer_copy_updates_a_row_and_an_older_one_does_not(db):
    db.upsert_evidence([ev("human_miss", "o/r#1", "A", role="unknown")])
    assert dict(db.upsert_evidence([ev("human_miss", "o/r#1", "A", role="maintainer")])) == {"updated": 1}
    assert [e.role for e in db.evidence()] == ["maintainer"]
    assert dict(db.upsert_evidence([ev("human_miss", "o/r#1", "A", ts=T0 - 5, role="unknown")])) == {"unchanged": 1}
    assert [e.role for e in db.evidence()] == ["maintainer"]


def test_a_newer_signal_for_a_thread_replaces_the_older_one(db):
    db.upsert_evidence([ev("outdated", "o/r#1", "A", ts=T0)])
    out = db.upsert_evidence([ev("resolved_outdated", "o/r#1", "A", ts=T0 + DAY)])
    assert dict(out) == {"new": 1}
    assert [e.signal for e in db.evidence()] == ["resolved_outdated"]  # one thread, one row: never counted twice
    assert dict(db.upsert_evidence([ev("outdated", "o/r#1", "A", ts=T0)])) == {"stale": 1}
    assert [e.signal for e in db.evidence()] == ["resolved_outdated"]


def test_lessons_are_recomputed_from_evidence_rows_never_from_stored_totals(db):
    items = [ev("verdict_valid", "o/r#1", "A"), ev("verdict_valid", "o/r#2", "B"), ev("verdict_incorrect", "o/r#3", "C", role="author")]
    db.upsert_evidence(items)
    out = db.recompute(Params(), T0 + DAY)
    assert (out.added, out.changed, out.removed) == (1, 0, 0)
    (lesson,) = db.lessons()
    assert lesson == consolidate(items, T0 + DAY)[0]
    assert (lesson.status, lesson.support, lesson.contra, lesson.lean) == ("active", 2.0, 0.0, "confirm")
    # tampering with a stored lesson is undone by the next recompute
    conn = sqlite3.connect(db.file)
    conn.execute("UPDATE lessons SET support = 99, status = 'active'")
    conn.commit()
    conn.close()
    out = db.recompute(Params(), T0 + DAY)
    assert out.changed == 1 and db.lessons()[0].support == 2.0
    again = db.recompute(Params(), T0 + DAY)
    assert again.__dict__ == {"lessons": [lesson], "added": 0, "changed": 0, "removed": 0}


def test_list_and_get_by_scope(db):
    other = Scope("github.com", "o/other", "lib/**", "go", "docs")
    db.upsert_evidence([ev("verdict_valid", "o/r#1", "A"), ev("verdict_valid", "o/x#1", "B", scope=other),
                        ev("human_miss", "o/r#1", "M", scope=replace(SCOPE, category="any"))])
    db.recompute(Params(), T0 + DAY)
    assert len(db.lessons()) == 3
    assert [le.scope.repo for le in db.lessons(repo="o/other")] == ["o/other"]
    assert [le.kind for le in db.lessons(repo="o/r", kind="miss")] == ["miss"]
    assert db.lessons(language="python", category="tests")[0].scope == SCOPE
    assert db.lessons(status="active") == []
    lesson = db.lessons(repo="o/other")[0]
    assert db.lesson(lesson.id) == lesson and db.lesson("0" * 16) is None


def test_forget_removes_the_lesson_and_its_evidence_and_it_stays_gone(db):
    db.upsert_evidence([ev("verdict_valid", "o/r#1", "A"), ev("verdict_valid", "o/r#2", "B")])
    db.recompute(Params(), T0 + DAY)
    (lesson,) = db.lessons()
    assert db.forget(lesson.id) == {"lesson": True, "evidence": 2}
    assert db.lessons() == [] and db.evidence() == []
    assert dict(db.upsert_evidence([ev("verdict_valid", "o/r#1", "A")])) == {"forgotten": 1}  # not re-learned from the shadow store
    assert db.forget(lesson.id) == {"lesson": False, "evidence": 0}
    assert db.status()["forgotten"] == 1


def test_expire_deletes_old_evidence_and_the_lessons_it_supported(db):
    db.upsert_evidence([ev("verdict_valid", "o/r#1", "A", ts=T0), ev("verdict_valid", "o/r#2", "B", ts=T0 + 150 * DAY)])
    p = Params()
    db.recompute(p, T0 + 181 * DAY)
    assert db.lessons()[0].status == "candidate"  # one item decayed to half, the other live
    assert db.expire(p, T0 + 181 * DAY) == 1
    assert len(db.evidence()) == 1
    assert db.expire(p, T0 + 400 * DAY) == 1 and db.lessons() == []


def test_decay_through_the_store_uses_the_injected_clock(db):
    db.upsert_evidence([ev("verdict_valid", "o/r#1", "A"), ev("verdict_valid", "o/r#2", "B")])
    db.recompute(Params(), T0 + 10 * DAY)
    assert db.lessons()[0].status == "active"
    db.recompute(Params(), T0 + 100 * DAY)
    assert db.lessons()[0].status == "candidate"
    db.recompute(Params(), T0 + 200 * DAY)
    assert db.lessons()[0].status == "expired"


def test_a_corrupt_file_raises_a_clear_error_and_never_a_bare_sqlite_error(tmp_path):
    d = tmp_path / "mem"
    d.mkdir()
    (d / "memory.db").write_bytes(b"this is not a database" * 100)
    store = MemoryDB(d)
    for call_ in (store.lessons, store.evidence, store.status, lambda: store.recompute(Params(), T0)):
        with pytest.raises(MemoryStoreError, match="unreadable"):
            call_()
    with pytest.raises(MemoryStoreError, match="unreadable"):
        store.upsert_evidence([ev("verdict_valid", "o/r#1", "A")])


def test_a_newer_schema_or_a_foreign_database_is_refused(tmp_path):
    d = tmp_path / "mem"
    d.mkdir()
    conn = sqlite3.connect(d / "memory.db")
    conn.execute(f"PRAGMA user_version = {dbmod.SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    with pytest.raises(MemoryStoreError, match="schema"):
        MemoryDB(d).lessons()
    foreign = tmp_path / "other"
    foreign.mkdir()
    conn = sqlite3.connect(foreign / "memory.db")
    conn.execute("CREATE TABLE x (a)")
    conn.commit()
    conn.close()
    with pytest.raises(MemoryStoreError, match="not a code-check memory store"):
        MemoryDB(foreign).upsert_evidence([ev("verdict_valid", "o/r#1", "A")])


def test_clip_scrubs_before_capping():
    secret = "ghp_" + "a" * 36
    assert clip(f"token {secret} here", 240) == "token [REDACTED] here"
    assert len(clip("x" * 1000, QUOTE_CAP)) == QUOTE_CAP
    assert clip("   ", 10) is None and clip(None, 10) is None and clip(5, 10) is None


# ---- the shadow store -> memory store command ----


def seed(shadow, clock, name, pr, *, day, humans=(), findings=(), repo="o/r"):
    """humans: (thread, path, role or None, matches_finding or None, body). findings: (id, path, category, outcome or None)."""
    rid = rid_of(name)
    clock.t = T0 + day * DAY
    shadow.append(rid, {"type": "run", "repo": repo, "pr": f"{repo}#{pr}", "tool_version": "1"})
    for fid, path, cat, outcome in findings:
        shadow.append(rid, {"type": "finding", "id": fid, "file": path, "category": cat, "message": "m" * 600, "severity": "Warning"})
        if outcome is not None:
            shadow.append(rid, {"type": "outcome", "finding_id": fid, "thread_id": f"T-{fid}", "replies": [], **outcome})
            if "verdict" in outcome:
                shadow.append(rid, {"type": "verdict", "thread_id": f"T-{fid}", "verdict": outcome["verdict"]})
    for thread, path, role, match, body in humans:
        rec = {"type": "human_comment", "thread_id": thread, "path": path, "line": 1, "author": "h", "body": body,
               "matches_finding": match}
        if role:
            rec["role"] = role
        shadow.append(rid, rec)


@pytest.fixture
def world(tmp_path, monkeypatch):
    _, clone = make_origin_and_clone(tmp_path)
    ctx = make_ctx(tmp_path, clone, memory=MemoryConfig(store=str(tmp_path / "mem")))
    clock = Clock()
    shadow = ShadowStore(tmp_path / "shadow", now=clock)
    monkeypatch.setattr("codecheck.commands.memory.clock", lambda: T0 + 5 * DAY)
    return ctx, shadow, clock, tmp_path


def shadow_snapshot(root):
    return sorted((p.name, p.read_bytes()) for p in root.iterdir())


def test_consolidate_weights_by_role_and_is_idempotent(world):
    ctx, shadow, clock, tmp = world
    seed(shadow, clock, "a1", 1, day=0, humans=[("T1", "src/app/a.py", "maintainer", None, "x" * 500)])
    seed(shadow, clock, "a2", 2, day=1, humans=[("T2", "src/app/b.py", "contributor", None, "please add a check")])
    before = shadow_snapshot(tmp / "shadow")
    code, first = call(ctx, "memory", "consolidate")
    assert code == 0
    assert first["evidence"] == {"new": 2, "updated": 0, "unchanged": 0, "stale": 0, "forgotten": 0, "skipped_expired": 0}
    assert first["lessons"]["total"] == 1 and first["lessons"]["by_status"] == {"candidate": 1}  # 1.0 + 0.5 is under 2.0
    assert shadow_snapshot(tmp / "shadow") == before  # the shadow store is only read
    code, second = call(ctx, "memory", "consolidate")
    assert code == 0 and second["evidence"]["new"] == 0 and second["evidence"]["unchanged"] == 2
    assert second["lessons"] | {"added": 0} == first["lessons"] | {"added": 0}
    assert (second["lessons"]["added"], second["lessons"]["changed"], second["lessons"]["removed"]) == (0, 0, 0)


def test_legacy_records_without_roles_still_consolidate_as_unknown(world):
    ctx, shadow, clock, tmp = world
    seed(shadow, clock, "a1", 1, day=0, humans=[("T1", "src/app/a.py", None, None, "old record")])
    seed(shadow, clock, "a2", 2, day=1, humans=[("T2", "src/app/b.py", None, None, "old record")])
    code, out = call(ctx, "memory", "consolidate")
    assert code == 0 and out["lessons"]["by_status"] == {"candidate": 1}  # 0.5 + 0.5, no maintainer item
    rows = MemoryDB(tmp / "mem").evidence()
    assert {e.role for e in rows} == {"unknown"} and {e.signal for e in rows} == {"human_miss"}


def test_a_maintainer_miss_in_two_prs_activates(world):
    ctx, shadow, clock, _tmp = world
    seed(shadow, clock, "a1", 1, day=0, humans=[("T1", "src/app/a.py", "maintainer", None, "b")])
    seed(shadow, clock, "a2", 2, day=1, humans=[("T2", "src/app/b.py", "maintainer", None, "b")])
    _, out = call(ctx, "memory", "consolidate")
    assert out["lessons"]["by_status"] == {"active": 1} and out["lessons"]["by_kind"] == {"miss": 1}


def test_local_text_is_scrubbed_and_capped(world):
    ctx, shadow, clock, tmp = world
    seed(shadow, clock, "a1", 1, day=0, humans=[("T1", "src/app/a.py", "maintainer", "f1", "y" * 900)],
         findings=[("f1", "src/app/a.py", "tests", None)])
    call(ctx, "memory", "consolidate")
    (row,) = MemoryDB(tmp / "mem").evidence()
    assert row.signal == "human_matched" and len(row.quote) == QUOTE_CAP
    assert len(row.finding["message"]) == MESSAGE_CAP and row.finding["severity"] == "Warning"


def test_it_works_with_memory_disabled_and_changes_no_review_state(world):
    ctx, shadow, clock, tmp = world
    assert ctx.config.memory.enabled is False
    seed(shadow, clock, "a1", 1, day=0, humans=[("T1", "src/app/a.py", "maintainer", None, "b")])
    code, out = call(ctx, "memory", "consolidate")
    assert code == 0 and out["lessons"]["total"] == 1
    assert not (tmp / "state" / "runs").exists()


def test_an_empty_shadow_store_creates_no_memory_store(world):
    ctx, _, _, tmp = world
    code, out = call(ctx, "memory", "consolidate")
    assert code == 0 and out["lessons"]["total"] == 0 and not (tmp / "mem").exists()


def test_evidence_past_expire_days_is_skipped_and_prune_removes_old_rows(world, monkeypatch):
    ctx, shadow, clock, tmp = world
    seed(shadow, clock, "a1", 1, day=0, humans=[("T1", "src/app/a.py", "maintainer", None, "b")])
    seed(shadow, clock, "a2", 2, day=4, humans=[("T2", "src/app/b.py", "maintainer", None, "b")])
    _, out = call(ctx, "memory", "consolidate")
    assert out["evidence"]["new"] == 2
    monkeypatch.setattr("codecheck.commands.memory.clock", lambda: T0 + 183 * DAY)  # a1 (day 0) is past 180, a2 is not
    _, again = call(ctx, "memory", "consolidate")
    assert again["evidence"]["skipped_expired"] == 1 and again["evidence"]["new"] == 0
    _, pruned = call(ctx, "memory", "consolidate", "--prune")
    assert pruned["pruned"] == 1 and len(MemoryDB(tmp / "mem").evidence()) == 1


def test_forget_survives_the_next_consolidate(world):
    ctx, shadow, clock, tmp = world
    seed(shadow, clock, "a1", 1, day=0, humans=[("T1", "src/app/a.py", "maintainer", None, "b")])
    call(ctx, "memory", "consolidate")
    store = MemoryDB(tmp / "mem")
    (lesson,) = store.lessons()
    store.forget(lesson.id)
    _, out = call(ctx, "memory", "consolidate")
    assert out["evidence"]["forgotten"] == 1 and out["lessons"]["total"] == 0


def test_a_corrupt_store_gives_a_json_error_not_a_crash(world):
    ctx, shadow, clock, tmp = world
    seed(shadow, clock, "a1", 1, day=0, humans=[("T1", "src/app/a.py", "maintainer", None, "b")])
    (tmp / "mem").mkdir()
    (tmp / "mem" / "memory.db").write_bytes(b"garbage" * 200)
    code, out = call(ctx, "memory", "consolidate")
    assert code == 1 and "unreadable" in out["error"]
    assert json.dumps(out)


def test_the_store_path_expands_the_home_directory(world, monkeypatch):
    ctx, shadow, clock, tmp = world
    monkeypatch.setenv("HOME", str(tmp / "home"))
    ctx = replace(ctx, config=replace(ctx.config, memory=MemoryConfig(store="~/lessons")))
    seed(shadow, clock, "a1", 1, day=0, humans=[("T1", "src/app/a.py", "maintainer", None, "b")])
    code, _out = call(ctx, "memory", "consolidate")
    assert code == 0 and (tmp / "home" / "lessons" / "memory.db").is_file()


def test_roles_recorded_by_collect_reach_the_evidence(world):
    _ctx, shadow, clock, _tmp = world
    seed(shadow, clock, "a1", 1, day=0, findings=[("f1", "src/app/a.py", "tests", {"resolved": True, "outdated": True,
                                                                                    "resolved_by_role": "maintainer"})])
    (run,) = load_runs(shadow)
    (e,) = run.evidence
    assert (e.signal, e.role) == ("resolved_outdated", "maintainer")
