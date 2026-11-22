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


# ---------- the semantic retriever in the retrieval eval ----------

STOP = {"a", "and", "be", "can", "has", "here", "is", "that", "the", "were", "with", "as", "no", "whether"}
CONCEPTS = {"nullable": "null", "none": "null", "checked": "check", "checks": "check", "retries": "retry"}


class ConceptEmbedder(FakeEmbedder):
    """The fake of test_memory_semantic with a tiny concept map, so related wordings share a dimension without a model."""

    def embed(self, texts, budget=5.0):
        import re

        folded = [" ".join(CONCEPTS.get(w, w) for w in re.findall(r"[a-z0-9]+", t.lower()) if w not in STOP) for t in texts]
        return super().embed(folded, budget)


def test_the_semantic_retriever_finds_what_exact_keys_miss_in_the_retrieval_eval(tmp_path, monkeypatch):
    from clihelp import make_ctx

    from codecheck.config import MemoryConfig
    from codecheck.evals import memory_retrieval, memory_semantic

    monkeypatch.setattr(memory_retrieval, "RETRIEVERS", dict(memory_retrieval.RETRIEVERS))
    cfg = MemoryConfig(store=str(tmp_path / "mem"), embed_model="fake-embed", min_similarity=0.3)
    ctx = make_ctx(tmp_path, tmp_path, memory=cfg)
    assert memory_semantic.register(ctx, ConceptEmbedder()) is True
    out = memory_retrieval.run_retrieval(memory_retrieval.DEFAULT_CASES)
    exact, sem = out["retrievers"]["exact"], out["retrievers"]["semantic"]
    print(f"recall@5 exact={exact['recall_at_5']} semantic={sem['recall_at_5']}; mrr exact={exact['mrr']} semantic={sem['mrr']}; "
          f"semantic tier exact={exact['by_tier']['semantic']['recall_at_5']} semantic={sem['by_tier']['semantic']['recall_at_5']}")
    assert exact["by_tier"]["semantic"]["recall_at_5"] == 0.0
    assert sem["by_tier"]["semantic"]["recall_at_5"] == 1.0
    assert sem["by_tier"]["exact"]["recall_at_5"] == 1.0  # exact-key hits are never lost
    assert sem["absent_violations"] == 0 and sem["render_failures"] == 0
    assert sem["recall_at_5"] > exact["recall_at_5"]
    per = {r["query"]: r for c in sem["per_case"].values() for r in c["results"]}
    assert per["sibling-directory-same-topic"]["recall_at_5"] == 1.0 and per["free-wording-for-validation"]["recall_at_5"] == 1.0


def test_the_semantic_retriever_is_registered_only_with_a_model_and_skips_when_unreachable(tmp_path, monkeypatch):
    from clihelp import make_ctx

    from codecheck.config import MemoryConfig
    from codecheck.errors import CodeCheckError
    from codecheck.evals import memory_retrieval, memory_semantic

    monkeypatch.setattr(memory_retrieval, "RETRIEVERS", dict(memory_retrieval.RETRIEVERS))
    off = make_ctx(tmp_path, tmp_path, memory=MemoryConfig(store=str(tmp_path / "mem")))
    assert memory_semantic.register(off, ConceptEmbedder()) is False and "semantic" not in memory_retrieval.RETRIEVERS
    on = make_ctx(tmp_path, tmp_path, memory=MemoryConfig(store=str(tmp_path / "mem"), embed_model="fake-embed"))
    assert memory_semantic.register(on, ConceptEmbedder(fail=True)) is True
    skipped = memory_retrieval.run_retrieval(memory_retrieval.DEFAULT_CASES, "semantic", if_reachable=True)
    assert skipped["retrievers"]["semantic"]["skipped"].startswith("not available")
    with pytest.raises(CodeCheckError, match="not available"):
        memory_retrieval.run_retrieval(memory_retrieval.DEFAULT_CASES, "semantic")
