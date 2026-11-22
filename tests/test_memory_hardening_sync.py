"""Hardening on the client side: purge propagation, pulling the repository under review, and what logs and errors may hold."""

import json

import pytest
from gitutil import make_origin_and_clone
from test_memory_server import NOW, Srv, make_ev
from test_memory_sync import Env, fake_server, url_of

from codecheck.memory import sync
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


def test_a_revoked_and_purged_contributors_evidence_leaves_teammates_replicas(tmp_path, clone, srv):
    carol = Env(tmp_path, "c", clone, url=url_of(srv), token=srv.carol).evidence(make_ev("T1"), make_ev("T2", pr="o/r#2"))
    bob = Env(tmp_path, "b", clone, url=url_of(srv), token=srv.bob).evidence(make_ev("T3", pr="o/r#3"))
    sync.sync(carol.ctx)
    sync.sync(bob.ctx)
    assert {e.thread for e in bob.db.evidence()} == {"T1", "T2", "T3"}
    srv.db.revoke_token("carol")
    srv.db.purge_contributor("carol", srv.service.params, NOW)
    out = sync.sync(bob.ctx, do_push=False)
    assert out["pull"]["repos"][0]["purged"] == 2
    assert {e.thread for e in bob.db.evidence()} == {"T3"}  # bob's own evidence is untouched
    assert bob.db.lessons()[0].support == 1.0  # recomputed from what is left
    assert sync.load_state(bob.ctx)["purge_seq"]["o/r"] >= 1
    assert not set(sync.load_state(bob.ctx)["pulled_ids"]) & {make_ev("T1").id, make_ev("T2", pr="o/r#2").id}


def test_a_server_cannot_make_a_client_delete_its_own_evidence(tmp_path, clone):
    me = Env(tmp_path, "a", clone, url="http://127.0.0.1:1", token="cck_x").evidence(make_ev("T1"))
    mine = [e.id for e in me.db.evidence()]
    body = json.dumps({"evidence": [], "next_cursor": None, "purged": mine + ["0" * 16, "../../etc", 7], "purged_seq": 5}).encode()
    httpd = fake_server(200, body)
    try:
        state: dict = {}
        out = sync.pull(me.ctx, HttpTransport(f"http://127.0.0.1:{httpd.server_port}", "cck_x"), ["o/r"], state, now=NOW)
        assert out["pulled"] and out["repos"][0].get("purged", 0) == 0
        assert [e.id for e in me.db.evidence()] == mine  # local-origin evidence is never removed by a purge list
        assert state["purge_seq"]["o/r"] == 5
    finally:
        httpd.shutdown()


def test_a_huge_or_malformed_purge_answer_is_capped_and_ignored(tmp_path, clone):
    me = Env(tmp_path, "a", clone, url="http://127.0.0.1:1", token="cck_x")
    ids = [f"{i:016x}" for i in range(sync.MAX_PURGED_IDS + 100)]
    got, seq = sync._parse_purged({"purged": ids, "purged_seq": 3})
    assert len(got) == sync.MAX_PURGED_IDS and seq == 3
    for bad in ({"purged": "x", "purged_seq": 1}, {"purged": [], "purged_seq": -1}, {"purged": [], "purged_seq": True}, {}):
        assert sync._parse_purged(bad) == ([], None)
    assert me.db.evidence() == []


def test_the_reviewed_repo_is_pulled_even_when_it_is_not_in_share_repos(tmp_path, clone, srv):
    a = Env(tmp_path, "a", clone, url=url_of(srv), token=srv.alice).evidence(make_ev("T1"))
    sync.sync(a.ctx)
    b = Env(tmp_path, "b", clone, url=url_of(srv), token=srv.bob, share=())  # shares nothing, may still read
    assert sync.pull_if_stale(b.ctx)["reason"] == "no repositories to pull"  # the old default path is unchanged
    out = sync.pull_if_stale(b.ctx, repos=["o/r"])
    assert out["pulled"] and {e.thread for e in b.db.evidence()} == {"T1"}
    assert sync.pull_if_stale(b.ctx, repos=["o/r"])["reason"] == "replica is fresh"  # freshness is per repository
    assert sync.pull_if_stale(b.ctx, repos=["o/other"])["pulled"] is True
    assert sync.push(b.ctx, sync.build_transport(b.ctx), {}, now=NOW)["pushed"] is False  # push still needs share_repos


def test_a_malformed_repo_name_is_never_sent(tmp_path, clone):
    me = Env(tmp_path, "a", clone, url="http://127.0.0.1:1", token="cck_x", share=())
    assert sync.pull_if_stale(me.ctx, repos=["-bad", "a b", ""]) == {"pulled": False, "reason": "no repositories to pull"}


def test_context_pull_passes_the_repo_under_review(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from codecheck.memory import inject

    seen = {}
    monkeypatch.setattr(sync, "pull_if_stale", lambda ctx, **kw: seen.update(kw) or {"pulled": True})
    ctx = SimpleNamespace(config=SimpleNamespace(memory=SimpleNamespace(url="http://127.0.0.1:1")))
    inject._pull_replica(ctx, inject.REVIEWERS, 2.0, "o/r")
    assert seen == {"repos": ["o/r"]}


def test_an_unknown_path_is_never_logged(srv):
    srv.req("GET", f"/{srv.alice}/anything", token="cck_wrong")
    assert not any(srv.alice in line or "anything" in line for line in srv.lines)
    assert any("/other 401" in line for line in srv.lines)


def test_errors_and_state_never_hold_the_token(tmp_path, clone, srv):
    me = Env(tmp_path, "a", clone, url="http://127.0.0.1:1", token="cck_secrettoken").evidence(make_ev("T1", quote="SECRET QUOTE"))
    out = sync.sync(me.ctx)
    assert "cck_secrettoken" not in json.dumps(out) and "SECRET QUOTE" not in json.dumps(out)
    assert "cck_secrettoken" not in (tmp_path / "a" / "mem" / sync.STATE_FILE).read_text()
