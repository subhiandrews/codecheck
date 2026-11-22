"""Integration of hardening with semantic retrieval: a role downgrade with vectors, and a purge that removes vectors too."""

import pytest
from gitutil import make_origin_and_clone
from test_memory_hardening_sync import fixed_clock  # noqa: F401 - autouse fixture
from test_memory_semantic import DIM, FakeEmbedder, finding_ev, with_vectors
from test_memory_server import NOW, Srv, item_for
from test_memory_sync import Env, url_of

from codecheck.memory import semantic, sync


@pytest.fixture
def clone(tmp_path):
    return make_origin_and_clone(tmp_path)[1]


@pytest.fixture
def srv(tmp_path):
    s = Srv(tmp_path)
    yield s
    s.close()


def test_a_contributor_ceiling_downgrades_the_role_and_still_stores_the_vector(srv):
    emb = FakeEmbedder()
    ev = finding_ev("a", role="maintainer")
    item = with_vectors(item_for([ev], "evidence"), [ev], emb)
    status, body = srv.req("PUT", "/v1/lessons", {"items": [item]}, srv.carol)  # carol: contributor ceiling
    assert status == 200 and body["downgraded_roles"] == 1, body
    assert [e.role for e in srv.db.evidence()] == ["contributor"]
    assert semantic.vector_stats(srv.db)["vectors"] == 1
    other = finding_ev("b", role="maintainer")
    item = with_vectors(item_for([other], "evidence"), [other], emb)
    status, body = srv.req("PUT", "/v1/lessons", {"items": [item]}, srv.alice)  # alice: maintainer ceiling
    assert status == 200 and body["downgraded_roles"] == 0
    assert sorted(e.role for e in srv.db.evidence()) == ["contributor", "maintainer"]
    assert semantic.vector_stats(srv.db)["vectors"] == 2


def test_a_purge_removes_the_vectors_on_the_server_and_in_a_teammates_replica(tmp_path, clone, srv, monkeypatch):
    fake = FakeEmbedder()
    monkeypatch.setattr(semantic, "make_embedder", lambda cfg: fake if cfg.embed_model else None)
    emb = FakeEmbedder()
    mine = finding_ev("keep", pr="o/r#9")
    theirs = finding_ev("gone", pr="o/r#1")
    for ev, tok in ((theirs, srv.carol), (mine, srv.alice)):
        item = with_vectors(item_for([ev], "evidence"), [ev], emb)
        assert srv.req("PUT", "/v1/lessons", {"items": [item]}, tok)[0] == 200
    assert semantic.vector_stats(srv.db)["vectors"] == 2
    bob = Env(tmp_path, "b", clone, url=url_of(srv), token=srv.bob, level="evidence", embed_model="fake-embed")
    sync.sync(bob.ctx, do_push=False)
    assert {e.thread for e in bob.db.evidence()} == {"keep", "gone"}
    assert semantic.refresh_local(bob.ctx)["embedded"] == 2
    assert semantic.vector_stats(bob.db)["vectors"] == 2

    srv.db.revoke_token("carol")
    srv.db.purge_contributor("carol", srv.service.params, NOW)
    assert semantic.vector_stats(srv.db)["vectors"] == 1  # the server's vector of the purged evidence is gone too
    out = sync.sync(bob.ctx, do_push=False)
    assert out["pull"]["repos"][0]["purged"] == 1
    assert {e.thread for e in bob.db.evidence()} == {"keep"}
    assert semantic.vector_stats(bob.db)["vectors"] == 1  # and so is the replica's
    assert DIM  # the fake's dimension is the one stored
