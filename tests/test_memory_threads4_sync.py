"""Pulled-evidence origin survives a corrupt sync.json; a non-finite server watermark is ignored."""

import json

import pytest
from gitutil import make_origin_and_clone
from test_memory_server import NOW, Srv, make_ev
from test_memory_sync import Env, fake_server, url_of

from codecheck.memory import levels, sync
from codecheck.memory.client import HttpTransport


@pytest.fixture
def clone(tmp_path):
    return make_origin_and_clone(tmp_path)[1]


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(sync, "clock", lambda: NOW)


@pytest.fixture
def srv(tmp_path):
    s = Srv(tmp_path)
    yield s
    s.close()


def _corrupt(env):
    (sync._store_dir(env.ctx) / sync.STATE_FILE).write_text("{not json", encoding="utf-8")


def test_a_corrupt_sync_json_does_not_make_pulled_evidence_look_local(tmp_path, clone, srv):
    a = Env(tmp_path, "a", clone, url=url_of(srv), token=srv.alice).evidence(make_ev("T1"))
    b = Env(tmp_path, "b", clone, url=url_of(srv), token=srv.bob)
    sync.sync(a.ctx)
    sync.sync(b.ctx)  # b pulls T1
    assert {e.thread for e in b.db.evidence()} == {"T1"}
    _corrupt(b)
    sync.sync(b.ctx)
    srv.db.purge_contributor("alice", srv.service.params, NOW)
    assert srv.db.evidence() == []  # b never claimed alice's evidence


def test_a_lost_sync_json_still_lets_a_purge_remove_pulled_rows_only(tmp_path, clone, srv):
    a = Env(tmp_path, "a", clone, url=url_of(srv), token=srv.alice).evidence(make_ev("T1"))
    b = Env(tmp_path, "b", clone, url=url_of(srv), token=srv.bob).evidence(make_ev("T2", pr="o/r#2"))
    sync.sync(a.ctx)
    sync.sync(b.ctx)
    (sync._store_dir(b.ctx) / sync.STATE_FILE).unlink()
    srv.db.purge_contributor("alice", srv.service.params, NOW)
    sync.sync(b.ctx, do_push=False)
    assert {e.thread for e in b.db.evidence()} == {"T2"}


def test_ids_recorded_only_in_an_old_sync_json_are_adopted(tmp_path, clone, srv):
    a = Env(tmp_path, "a", clone, url=url_of(srv), token=srv.alice).evidence(make_ev("T1"))
    b = Env(tmp_path, "b", clone, url=url_of(srv), token=srv.bob)
    sync.sync(a.ctx)
    sync.sync(b.ctx)
    b.db.meta_set("pulled_ids", "[]")  # a store from before the record existed
    sync.sync(b.ctx)  # sync.json still lists them: they are written back to the store
    _corrupt(b)
    sync.sync(b.ctx)
    srv.db.purge_contributor("alice", srv.service.params, NOW)
    assert srv.db.evidence() == []


@pytest.mark.parametrize("bad", ["Infinity", "-1", "true", "NaN"])
def test_a_non_finite_or_invalid_received_ts_does_not_move_the_watermark(tmp_path, clone, bad):
    me = Env(tmp_path, "a", clone, url="http://127.0.0.1:1", token="cck_x")
    item = json.dumps({**levels.evidence_payload(make_ev("T1"), "episodes"), "lesson_id": "x"})
    raw = ('{"evidence": [' + item[:-1] + ', "received_ts": ' + bad + '}], "next_cursor": null}').encode()
    httpd = fake_server(200, raw)
    try:
        state: dict = {}
        sync.pull(me.ctx, HttpTransport(f"http://127.0.0.1:{httpd.server_port}", "cck_x"), ["o/r"], state, now=NOW)
        assert state["pull_since"].get("o/r", 0.0) == 0.0
    finally:
        httpd.shutdown()
