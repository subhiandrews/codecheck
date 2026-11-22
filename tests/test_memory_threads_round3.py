"""Review fixes, round three: vectors of purged evidence, and the vector write inside the ingest/purge lock."""

import threading

import pytest
from test_memory_semantic import DIGEST, DIM, FakeEmbedder, finding_ev, item_for, with_vectors
from test_memory_server import NOW, Srv

from codecheck.memory import embed, semantic
from codecheck.memory.embed import Pin


@pytest.fixture
def srv(tmp_path):
    s = Srv(tmp_path / "s")
    yield s
    s.close()


def test_a_vector_for_purged_evidence_is_not_stored(srv):
    ev = finding_ev("T1")
    srv.db.ingest("carol", [ev], srv.service.params, NOW)
    srv.db.purge_contributor("carol", srv.service.params, NOW)
    pin = Pin("fake-embed", DIGEST, DIM)
    stored = semantic.put_vectors(srv.db, [(ev.id, pin, embed.pack([1.0] + [0.0] * (DIM - 1), DIM))])
    assert stored == 0
    assert semantic.vector_stats(srv.db)["vectors"] == 0


def test_a_vector_for_existing_evidence_is_still_stored(srv):
    ev = finding_ev("T1")
    srv.db.ingest("carol", [ev], srv.service.params, NOW)
    pin = Pin("fake-embed", DIGEST, DIM)
    assert semantic.put_vectors(srv.db, [(ev.id, pin, embed.pack([1.0] + [0.0] * (DIM - 1), DIM))]) == 1
    assert semantic.vector_stats(srv.db)["vectors"] == 1


def test_the_put_lessons_vector_write_holds_the_ingest_purge_lock(srv, monkeypatch):
    emb = FakeEmbedder()
    ev = finding_ev("T1")
    item = with_vectors(item_for([ev], "evidence"), [ev], emb)
    real = semantic.put_vectors
    seen = {}

    def spy(db, rows):
        got = []
        t = threading.Thread(target=lambda: got.append(db._ingest_purge_lock.acquire(timeout=0.3)))
        t.start()
        t.join(2)
        if got and got[0]:  # another thread could take the lock: the write was not inside it
            db._ingest_purge_lock.release()
        seen["other_thread_got_lock"] = bool(got and got[0])
        return real(db, rows)

    monkeypatch.setattr(semantic, "put_vectors", spy)
    status, _ = srv.req("PUT", "/v1/lessons", {"items": [item]}, srv.alice)
    assert status == 200
    assert seen == {"other_thread_got_lock": False}
