"""Review fixes for the memory server and sync client: purge scope, purge-log gaps, ingest vs purge, sync.json lock, transport."""

import http.client
import io
import threading
import urllib.error

import pytest
from gitutil import make_origin_and_clone
from test_memory_server import NOW, Srv, make_ev
from test_memory_sync import Env, url_of

from codecheck.core.state import FileLock
from codecheck.memory import server, sync
from codecheck.memory.client import HttpTransport, MemoryTransportError
from codecheck.memory.model import Scope


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


def _contrib_less(db):
    with db._use(write=False) as conn:
        return [r[0] for r in conn.execute(
            "SELECT id FROM evidence WHERE id NOT IN (SELECT evidence_id FROM evidence_contrib)")]


def test_a_purge_cannot_slip_between_ingest_steps_and_orphan_evidence(srv, monkeypatch):
    db, ev = srv.db, make_ev("T1")
    real = type(db).upsert_evidence
    threads = []

    def upsert(self, items):
        t = threading.Thread(target=lambda: db.purge_contributor("carol", srv.service.params, NOW))
        t.start()
        t.join(0.5)  # unserialised, the purge completes here, between the contribution row and the evidence
        threads.append(t)
        return real(self, items)

    monkeypatch.setattr(type(db), "upsert_evidence", upsert)
    db.ingest("carol", [ev], srv.service.params, NOW)
    threads[0].join(5)
    assert _contrib_less(db) == []  # evidence is either gone with its contributor or still reachable by a purge


def test_purging_one_contributor_leaves_other_rows_alone(srv):
    srv.db.ingest("carol", [make_ev("T1")], srv.service.params, NOW)
    stray = make_ev("T2", pr="x/y#1", scope=Scope("github.com", "x/y", "src/app/**", "python", "tests"))
    srv.db.upsert_evidence([stray])  # no contribution row: not carol's
    srv.db.purge_contributor("carol", srv.service.params, NOW)
    assert {e.id for e in srv.db.evidence()} == {stray.id}
    with srv.db._use(write=False) as conn:
        logged = {r[0] for r in conn.execute("SELECT evidence_id FROM purge_log")}
    assert logged == {make_ev("T1").id}


def test_a_non_ascii_digit_content_length_is_411_not_500(srv):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    conn.putrequest("PUT", "/v1/lessons")
    conn.putheader("Authorization", f"Bearer {srv.alice}")
    conn.putheader("Content-Length", "²")
    conn.endheaders()
    assert conn.getresponse().status == 411
    conn.close()


def test_a_client_behind_a_pruned_purge_log_drops_what_it_pulled_and_repulls(tmp_path, clone, srv, monkeypatch):
    dave = Env(tmp_path, "d", clone, url=url_of(srv), token=srv.db.add_token("dave")).evidence(make_ev("T0", pr="o/r#9"))
    carol = Env(tmp_path, "c", clone, url=url_of(srv), token=srv.carol).evidence(make_ev("T1"))
    erin = Env(tmp_path, "e", clone, url=url_of(srv), token=srv.db.add_token("erin")).evidence(make_ev("T9", pr="o/r#8"))
    bob = Env(tmp_path, "b", clone, url=url_of(srv), token=srv.bob).evidence(make_ev("T3", pr="o/r#3"))
    for env in (dave, carol, erin):
        sync.sync(env.ctx)
    srv.db.purge_contributor("dave", srv.service.params, NOW)  # sequence 1
    sync.sync(bob.ctx)
    assert sync.load_state(bob.ctx)["purge_seq"]["o/r"] == 1
    assert "T1" in {e.thread for e in bob.db.evidence()}
    monkeypatch.setattr(server, "PURGE_LOG_MAX", 1)
    srv.db.purge_contributor("carol", srv.service.params, NOW)  # sequence 2: bob misses it...
    srv.db.purge_contributor("erin", srv.service.params, NOW)  # ...and sequence 3 prunes it from the log
    out = sync.sync(bob.ctx, do_push=False)
    assert out["ok"]
    assert {e.thread for e in bob.db.evidence()} == {"T3"}  # T1 is gone, and bob's own evidence stays
    assert sync.load_state(bob.ctx)["purge_seq"]["o/r"] == 3


def test_sync_state_is_not_touched_while_another_run_holds_the_lock(tmp_path, clone, srv):
    me = Env(tmp_path, "a", clone, url=url_of(srv), token=srv.alice).evidence(make_ev("T1"))
    sync._store_dir(me.ctx).mkdir(parents=True, exist_ok=True)
    lock = FileLock(sync._store_dir(me.ctx) / "sync.json.lock")
    assert lock.acquire()
    try:
        out = sync.pull_if_stale(me.ctx)
        assert out["pulled"] is False and "reason" in out
        assert sync.sync(me.ctx)["ok"] is False
        assert sync.load_state(me.ctx) == {}
    finally:
        lock.release()
    assert sync.sync(me.ctx)["ok"] is True
    assert not (sync._store_dir(me.ctx) / "sync.json.lock").exists()  # released afterwards


def test_an_http_error_body_that_fails_to_read_is_a_transport_error(monkeypatch):
    class Broken(io.BytesIO):
        def read(self, *a):
            raise http.client.IncompleteRead(b"")

    err = urllib.error.HTTPError("http://127.0.0.1:1/v1/meta", 500, "x", {}, Broken())  # type: ignore[arg-type]

    class Opener:
        def open(self, *a, **k):
            raise err

    monkeypatch.setattr(HttpTransport, "_opener", lambda self: Opener())
    with pytest.raises(MemoryTransportError):
        HttpTransport("http://127.0.0.1:1", "cck_x").meta()
