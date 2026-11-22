"""Review fixes for the memory store: sync origin, evidence cap, signal ties, revoke params, miss lesson ids."""

import dataclasses

import pytest
from clihelp import call
from gitutil import make_origin_and_clone
from test_memory_server import NOW, Srv, make_ev
from test_memory_sync import Env, token_file, url_of

from codecheck.memory import levels, sync
from codecheck.memory.consolidate import DAY, Params, consolidate
from codecheck.memory.db import MemoryDB
from codecheck.memory.server import ServerDB


@pytest.fixture
def clone(tmp_path):
    return make_origin_and_clone(tmp_path)[1]


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(sync, "clock", lambda: NOW)


def test_evidence_pulled_from_one_server_is_not_pushed_to_another(tmp_path, clone):
    a_dir, b_dir = tmp_path / "sa", tmp_path / "sb"
    a_dir.mkdir()
    b_dir.mkdir()
    srv_a, srv_b = Srv(a_dir), Srv(b_dir)
    try:
        carol = Env(tmp_path, "c", clone, url=url_of(srv_a), token=srv_a.alice).evidence(make_ev("T1"))
        assert sync.sync(carol.ctx)["ok"]
        me = Env(tmp_path, "me", clone, url=url_of(srv_a), token=srv_a.bob)
        assert sync.sync(me.ctx)["ok"]
        assert {e.thread for e in me.db.evidence()} == {"T1"}  # pulled, not ours
        mem = dataclasses.replace(me.ctx.config.memory, url=url_of(srv_b), token_file=token_file(tmp_path / "tb", srv_b.bob))
        object.__setattr__(me.ctx, "config", dataclasses.replace(me.ctx.config, memory=mem))
        assert sync.sync(me.ctx)["ok"]
        assert srv_b.db.evidence() == []
    finally:
        srv_a.close()
        srv_b.close()


def test_the_newest_evidence_is_kept_when_a_lesson_exceeds_the_cap():
    n = levels.MAX_EVIDENCE_PER_ITEM + 5
    evs = [make_ev(f"T{i}", pr=f"o/r#{i}", ts=NOW - 100000 + i) for i in range(n)]
    item = levels.lesson_item(evs[0].lesson_id, "finding", evs, "lessons")
    rows = item["evidence"]
    assert len(rows) == levels.MAX_EVIDENCE_PER_ITEM
    assert rows[-1]["ts"] == evs[-1].ts and rows[0]["ts"] == evs[5].ts
    before = levels.digest(item)
    evs.append(make_ev("TNEW", pr="o/r#999", ts=NOW - 10))
    assert levels.digest(levels.lesson_item(evs[0].lesson_id, "finding", evs, "lessons")) != before


@pytest.mark.parametrize("order", [("resolved", "reply"), ("reply", "resolved")])
def test_same_ts_signals_resolve_by_strength_not_arrival(tmp_path, order):
    db = MemoryDB(tmp_path / "m")
    for sig in order:
        db.upsert_evidence([make_ev("T1", signal=sig, ts=NOW - 50)])
    assert [e.signal for e in db.evidence()] == ["resolved"]


def test_revoke_purge_uses_the_configured_params(tmp_path, clone, monkeypatch):
    me = Env(tmp_path, "a", clone, half_life_days=7, expire_days=11)
    seen = []
    monkeypatch.setattr(ServerDB, "purge_contributor", lambda self, name, params, now: seen.append(params) or {"evidence": 0})
    db = str(tmp_path / "srvdb")
    ServerDB(tmp_path / "srvdb").init().add_token("dave")
    code, _ = call(me.ctx, "memory", "token", "revoke", "dave", "--purge", "--db", db)
    assert code == 0 and seen == [Params(half_days=7.0, expire_days=11.0)]


def test_an_all_expired_miss_group_keeps_the_evidence_lesson_id():
    ev = make_ev("T1", signal="human_miss", ts=NOW - 400 * DAY)
    (lesson,) = consolidate([ev], NOW, Params())
    assert lesson.kind == "miss" and lesson.id == ev.lesson_id
