"""The memory client, sync and the CLI handlers, against a real loopback server and small fake ones."""

import json
import os
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from clihelp import call, make_ctx
from fakes import FakeProvider
from gitutil import make_origin_and_clone
from test_memory_server import NOW, Srv, make_ev

from codecheck.config import MemoryConfig
from codecheck.errors import CodeCheckError
from codecheck.memory import sync
from codecheck.memory.client import (
    HttpTransport,
    LocalTransport,
    MemoryTransportError,
    check_url,
    read_token_file,
)
from codecheck.memory.db import MemoryDB
from codecheck.memory.model import Scope
from codecheck.memory.server import ServerDB, Service
from codecheck.shadow.store import ShadowStore

SECRET = "ghp_" + "a" * 36


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(sync, "clock", lambda: NOW)


@pytest.fixture
def srv(tmp_path):
    s = Srv(tmp_path)
    yield s
    s.close()


def token_file(path, token):
    path.write_text(token + "\n")
    os.chmod(path, 0o600)
    return str(path)


class Env:
    """A store directory with its own config: one 'machine'."""

    def __init__(self, tmp_path, name, clone, *, url="", token="", share=("o/r",), level="lessons", private_level="lessons",
                 public=True, **mem):
        self.root = tmp_path / name
        self.root.mkdir()
        self.provider = FakeProvider()
        self.provider.private = (not public) if public is not None else None
        tf = token_file(self.root / "token", token) if token else ""
        cfg = MemoryConfig(store=str(self.root / "mem"), url=url, token_file=tf, share_level=level, private_level=private_level,
                           share_repos=tuple(share), **mem)
        self.ctx = make_ctx(self.root, clone, provider=self.provider, memory=cfg)
        self.db = MemoryDB(self.root / "mem")

    def evidence(self, *evs):
        self.db.upsert_evidence(evs)
        return self


@pytest.fixture
def clone(tmp_path):
    return make_origin_and_clone(tmp_path)[1]


def url_of(s):
    return f"http://127.0.0.1:{s.port}"


# ---- client safety ----

def test_http_to_a_remote_host_and_credentials_in_the_url_are_refused():
    for bad in ("http://example.com", "http://10.0.0.5:8787", "https://user:pw@example.com", "ftp://x", "http://", "https://a b.com"):
        with pytest.raises(CodeCheckError):
            check_url(bad)
    for good in ("http://127.0.0.1:1", "http://localhost:1", "http://[::1]:1", "https://example.com/base/"):
        check_url(good)


def test_a_loose_token_file_is_refused_and_errors_never_show_the_token(tmp_path):
    p = tmp_path / "t"
    p.write_text("cck_supersecret\n")
    os.chmod(p, 0o644)
    with pytest.raises(CodeCheckError, match="chmod 600") as e:
        read_token_file(str(p))
    assert "cck_supersecret" not in str(e.value)
    os.chmod(p, 0o600)
    assert read_token_file(str(p)) == "cck_supersecret"
    p.write_text("two words\n")
    with pytest.raises(CodeCheckError):
        read_token_file(str(p))
    with pytest.raises(CodeCheckError, match="not set"):
        read_token_file("")
    assert "cck_supersecret" not in repr(HttpTransport("http://127.0.0.1:1", "cck_supersecret"))


class Fake(BaseHTTPRequestHandler):
    status, body, headers = 200, b"{}", {}

    def do_GET(self):
        self.send_response(self.status)
        for k, v in self.headers_out.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    do_PUT = do_GET

    def log_message(self, *a):
        pass


def fake_server(status=200, body=b"{}", headers=None):
    handler = type("H", (Fake,), {"status": status, "body": body, "headers_out": headers or {}})
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


@pytest.mark.parametrize("body", [b"not json at all", b"[1, 2]", b'{"half": ', b"\xff\xfe", b""])
def test_a_malformed_success_answer_is_a_transport_error(body):
    httpd = fake_server(200, body)
    try:
        with pytest.raises(MemoryTransportError):
            HttpTransport(f"http://127.0.0.1:{httpd.server_port}", "tok").meta()
    finally:
        httpd.shutdown()


def test_a_malformed_error_answer_is_still_a_response_and_redirects_are_not_followed():
    httpd = fake_server(500, b"<html>boom</html>")
    try:
        r = HttpTransport(f"http://127.0.0.1:{httpd.server_port}", "tok").meta()
        assert r.status == 500 and "error" in r.body
    finally:
        httpd.shutdown()
    hit = fake_server(200, b"{}")
    redir = fake_server(302, b"", {"Location": f"http://127.0.0.1:{hit.server_port}/"})
    try:
        with pytest.raises(MemoryTransportError, match="redirect"):
            HttpTransport(f"http://127.0.0.1:{redir.server_port}", "tok").meta()
    finally:
        hit.shutdown()
        redir.shutdown()


def test_a_dropped_connection_and_an_oversized_answer_are_transport_errors():
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    threading.Thread(target=lambda: (s.accept()[0].close(), s.close()), daemon=True).start()
    with pytest.raises(MemoryTransportError) as e:  # http.client.RemoteDisconnected is an HTTPException
        HttpTransport(f"http://127.0.0.1:{s.getsockname()[1]}", "cck_tok").meta()
    assert "cck_tok" not in str(e.value)
    big = fake_server(200, b'{"x": "' + b"a" * (2 * 1024 * 1024) + b'"}')
    try:
        with pytest.raises(MemoryTransportError, match="too large"):
            HttpTransport(f"http://127.0.0.1:{big.server_port}", "tok").meta()
    finally:
        big.shutdown()


def test_an_unreachable_server_error_has_no_token():
    with pytest.raises(MemoryTransportError) as e:
        HttpTransport("http://127.0.0.1:1", "cck_leaky", timeout=1).meta()
    assert "cck_leaky" not in str(e.value)


# ---- pushing ----

def test_nothing_is_pushed_when_share_repos_is_empty(tmp_path, clone, srv):
    me = Env(tmp_path, "a", clone, url=url_of(srv), token=srv.alice, share=()).evidence(make_ev("T1"))
    out = sync.sync(me.ctx)
    assert out["push"]["pushed"] is False and "share_repos" in out["push"]["reason"]
    assert srv.db.evidence() == []
    assert not any("PUT" in line or "meta" in line for line in srv.lines)  # not even a request


def test_a_repo_that_is_not_listed_is_not_pushed(tmp_path, clone, srv):
    other = Scope("github.com", "o/other", "src/**", "python", "tests")
    me = Env(tmp_path, "a", clone, url=url_of(srv), token=srv.alice).evidence(make_ev("T1"), make_ev("T9", scope=other))
    sync.sync(me.ctx, do_pull=False)
    assert {e.scope.repo for e in srv.db.evidence()} == {"o/r"}


@pytest.mark.parametrize("public", [True, False, None])
def test_private_and_unknown_repos_are_capped_at_lessons_public_ones_get_the_set_level(tmp_path, clone, srv, public):
    ev = make_ev("T1", quote="because reasons", finding={"severity": "warning", "message": "a message"})
    me = Env(tmp_path, "a", clone, url=url_of(srv), token=srv.alice, level="evidence", public=public).evidence(ev)
    out = sync.sync(me.ctx, do_pull=False)
    assert out["push"]["repos"][0]["level"] == ("evidence" if public else "lessons")
    stored = srv.db.evidence()[0]
    assert (stored.quote, stored.finding is not None) == (("because reasons", True) if public else (None, False))


def test_a_private_repo_can_be_explicitly_raised(tmp_path, clone, srv):
    me = Env(tmp_path, "a", clone, url=url_of(srv), token=srv.alice, level="evidence", private_level="evidence", public=False)
    me.evidence(make_ev("T1", quote="q"))
    assert sync.sync(me.ctx, do_pull=False)["push"]["repos"][0]["level"] == "evidence"
    assert srv.db.evidence()[0].quote == "q"


def test_quotes_are_scrubbed_before_they_leave_and_limited(tmp_path, clone, srv):
    evs = [make_ev(f"T{i}", pr=f"o/r#{i}", quote=f"leak {SECRET} " + "z" * 400, finding={"severity": "w", "message": f"m {SECRET}"})
           for i in range(5)]
    me = Env(tmp_path, "a", clone, url=url_of(srv), token=srv.alice, level="evidence").evidence(*evs)
    sync.sync(me.ctx, do_pull=False)
    got = srv.db.evidence()
    quotes = [e.quote for e in got if e.quote]
    assert len(quotes) == 3 and all(len(q) <= 240 and "ghp_" not in q for q in quotes)
    assert all("ghp_" not in (e.finding or {}).get("message", "") for e in got)


def test_level_episodes_sends_scrubbed_records_and_never_the_diff(tmp_path, clone, srv):
    me = Env(tmp_path, "a", clone, url=url_of(srv), token=srv.alice, level="episodes")
    shadow = ShadowStore(me.root / "shadow", now=lambda: NOW)
    rid = "c" * 12
    shadow.append(rid, {"type": "run", "repo": "o/r", "pr": "o/r#1"})
    shadow.append(rid, {"type": "finding", "id": "f1", "message": f"found {SECRET}", "diff": "DIFF-TEXT-SHOULD-NOT-LEAVE"})
    shadow.save_diff(rid, "+++ DIFF-TEXT-SHOULD-NOT-LEAVE\n")
    me.evidence(make_ev("T1"))
    out = sync.sync(me.ctx, do_pull=False)
    assert out["push"]["repos"][0]["episodes"]["sent"] == 1
    with srv.db._use(write=False) as conn:
        body = conn.execute("SELECT body_json FROM episodes").fetchone()[0]
    assert "DIFF-TEXT-SHOULD-NOT-LEAVE" not in body and "ghp_" not in body and "found" in body
    again = sync.sync(me.ctx, do_pull=False)
    assert again["push"]["repos"][0]["episodes"]["unchanged"] == 1


def test_a_level_b_client_against_a_level_a_server_is_capped_not_rejected(tmp_path, clone):
    s = Srv(tmp_path, max_level="lessons")
    try:
        me = Env(tmp_path, "a", clone, url=url_of(s), token=s.alice, level="episodes").evidence(make_ev("T1", quote="q"))
        out = sync.sync(me.ctx, do_pull=False)
        assert out["push"]["repos"][0]["level"] == "lessons" and out["ok"]
        assert s.db.evidence()[0].quote is None
    finally:
        s.close()


# ---- outbox ----

class Flaky:
    """Wraps a real transport, failing the first `fail` PUTs the given way."""

    def __init__(self, inner, mode, fail=1):
        self.inner, self.mode, self.fail, self.puts = inner, mode, fail, 0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def put_lessons(self, items):
        self.puts += 1
        if self.puts <= self.fail:
            if self.mode == "down":
                raise MemoryTransportError("down")
            from codecheck.memory.client import Response

            return Response(self.mode, {"error": "no"})
        return self.inner.put_lessons(items)


def test_a_transient_failure_is_retried_by_the_next_sync(tmp_path, clone, srv, monkeypatch):
    me = Env(tmp_path, "a", clone, url=url_of(srv), token=srv.alice).evidence(make_ev("T1"))
    real = sync.build_transport
    flaky = Flaky(real(me.ctx), 503)
    monkeypatch.setattr(sync, "build_transport", lambda *a, **k: flaky)
    first = sync.sync(me.ctx, do_pull=False)
    assert first["ok"] is False and "503" in first["push"]["error"] and srv.db.evidence() == []
    second = sync.sync(me.ctx, do_pull=False)
    assert second["ok"] and second["push"]["repos"][0]["lessons"]["sent"] == 1 and len(srv.db.evidence()) == 1
    third = sync.sync(me.ctx, do_pull=False)
    assert third["push"]["repos"][0]["lessons"] == {"sent": 0, "unchanged": 1, "refused": 0, "skipped_refused": 0}


def test_an_unreachable_server_is_reported_and_retried(tmp_path, clone, srv, monkeypatch):
    me = Env(tmp_path, "a", clone, url=url_of(srv), token=srv.alice).evidence(make_ev("T1"))
    flaky = Flaky(sync.build_transport(me.ctx), "down")
    monkeypatch.setattr(sync, "build_transport", lambda *a, **k: flaky)
    out = sync.sync(me.ctx, do_pull=False)
    assert out["ok"] is False
    assert "down" in out.get("error", "") or "down" in out["push"].get("error", "")
    assert sync.sync(me.ctx, do_pull=False)["ok"]


def test_a_permanent_rejection_does_not_block_later_items(tmp_path, clone, srv, monkeypatch):
    other = Scope("github.com", "o/r", "lib/**", "python", "tests")
    me = Env(tmp_path, "a", clone, url=url_of(srv), token=srv.alice).evidence(make_ev("T1"), make_ev("T2", scope=other))
    flaky = Flaky(sync.build_transport(me.ctx), 422)  # the first batch (both lessons) is refused for good
    monkeypatch.setattr(sync, "build_transport", lambda *a, **k: flaky)
    first = sync.sync(me.ctx, do_pull=False)
    assert first["push"]["repos"][0]["lessons"]["refused"] == 2
    # nothing changes: not resent, not retried forever, and the run did not fail
    second = sync.sync(me.ctx, do_pull=False)
    assert second["ok"] and second["push"]["repos"][0]["lessons"]["skipped_refused"] == 2 and flaky.puts == 1
    # a changed lesson is tried again, and a new lesson goes out in the same run
    me.evidence(make_ev("T3", pr="o/r#3"), make_ev("T4", scope=Scope("github.com", "o/r", "new/**", "go", "tests")))
    third = sync.sync(me.ctx, do_pull=False)
    assert third["ok"] and third["push"]["repos"][0]["lessons"]["sent"] == 2 and len(srv.db.evidence()) == 3  # T2's lesson is unchanged, so still not resent


def test_an_auth_failure_stops_the_run_without_marking_items_refused(tmp_path, clone, srv):
    bad = Env(tmp_path, "a", clone, url=url_of(srv), token="cck_not_a_token").evidence(make_ev("T1"))
    out = sync.sync(bad.ctx, do_pull=False)
    assert out["ok"] is False and "401" in out["error"] and "cck_not_a_token" not in json.dumps(out)
    good = replace(bad.ctx.config.memory, token_file=token_file(bad.root / "t2", srv.alice))
    bad.ctx.config = replace(bad.ctx.config, memory=good)
    assert sync.sync(bad.ctx, do_pull=False)["ok"] and len(srv.db.evidence()) == 1


# ---- two machines, one team ----

def test_two_machines_share_one_row_per_thread_and_pull_each_others_evidence(tmp_path, clone, srv):
    a = Env(tmp_path, "a", clone, url=url_of(srv), token=srv.alice).evidence(make_ev("T1", pr="o/r#1"), make_ev("T2", pr="o/r#2"))
    b = Env(tmp_path, "b", clone, url=url_of(srv), token=srv.bob).evidence(make_ev("T2", pr="o/r#2"), make_ev("T3", pr="o/r#3"))
    assert sync.sync(a.ctx)["ok"] and sync.sync(b.ctx)["ok"] and sync.sync(a.ctx)["ok"]
    assert len(srv.db.evidence()) == 3  # T2 once
    assert {e.thread for e in a.db.evidence()} == {"T1", "T2", "T3"}
    assert {e.thread for e in b.db.evidence()} == {"T1", "T2", "T3"}
    lesson = a.db.lessons()[0]
    assert lesson.support == 3.0 and lesson.n_prs == 3 and lesson.status == "active"
    assert srv.db.lessons()[0].support == 3.0  # the server and a replica agree


def test_pulled_evidence_is_not_pushed_back_under_the_pullers_name(tmp_path, clone, srv):
    a = Env(tmp_path, "a", clone, url=url_of(srv), token=srv.alice).evidence(make_ev("T1"))
    b = Env(tmp_path, "b", clone, url=url_of(srv), token=srv.bob)
    sync.sync(a.ctx)
    sync.sync(b.ctx)  # b pulls T1 and has nothing of its own
    assert {e.thread for e in b.db.evidence()} == {"T1"}
    sync.sync(b.ctx)
    srv.db.purge_contributor("alice", srv.service.params, NOW)
    assert srv.db.evidence() == []  # b never claimed it


def test_pull_ignores_a_server_that_answers_for_another_repo_or_sends_junk(tmp_path, clone):
    body = json.dumps({"evidence": [
        {"scope": {"host": "github.com", "repo": "evil/x", "path_glob": "a/**", "language": "", "category": "tests"},
         "signal": "verdict_valid", "role": "maintainer", "pr": "evil/x#1", "thread": "T", "ts": NOW - 5, "received_ts": NOW},
        {"nonsense": True}, "string"], "next_cursor": None}).encode()
    httpd = fake_server(200, body)
    try:
        me = Env(tmp_path, "a", clone, url=f"http://127.0.0.1:{httpd.server_port}", token="cck_x")
        out = sync.pull(me.ctx, HttpTransport(me.ctx.config.memory.url, "cck_x"), ["o/r"], {}, now=NOW)
        assert out["pulled"] and out["repos"][0]["evidence"] == 0 and out["repos"][0]["skipped"] == 3
        assert me.db.evidence() == []
    finally:
        httpd.shutdown()


def test_pull_if_stale_respects_age_and_falls_back_silently(tmp_path, clone, srv, monkeypatch):
    a = Env(tmp_path, "a", clone, url=url_of(srv), token=srv.alice).evidence(make_ev("T1"))
    b = Env(tmp_path, "b", clone, url=url_of(srv), token=srv.bob)
    sync.sync(a.ctx)
    first = sync.pull_if_stale(b.ctx)
    assert first["pulled"] and len(b.db.evidence()) == 1
    assert sync.pull_if_stale(b.ctx)["reason"] == "replica is fresh"
    assert sync.pull_if_stale(b.ctx, max_age_seconds=0)["pulled"] is True
    # a dead server: no exception, no change, and no retry storm
    srv.close()
    monkeypatch.setattr(sync, "clock", lambda: NOW + 10_000)
    dead = sync.pull_if_stale(b.ctx)
    assert dead["pulled"] is False and len(b.db.evidence()) == 1
    assert sync.pull_if_stale(b.ctx)["reason"] == "a recent pull failed"


def test_pull_if_stale_does_nothing_without_a_url_and_swallows_a_bad_token_file(tmp_path, clone):
    solo = Env(tmp_path, "a", clone)
    assert sync.pull_if_stale(solo.ctx) == {"pulled": False, "reason": "no memory.url"}
    broken = Env(tmp_path, "b", clone, url="http://127.0.0.1:1")  # no token file configured
    assert sync.pull_if_stale(broken.ctx)["pulled"] is False


# ---- solo mode ----

def test_solo_mode_syncs_through_a_local_sqlite_file_with_no_daemon(tmp_path, clone):
    hub = tmp_path / "hub"
    a = Env(tmp_path, "a", clone).evidence(make_ev("T1", pr="o/r#1"))
    b = Env(tmp_path, "b", clone).evidence(make_ev("T2", pr="o/r#2"))
    out = sync.sync(a.ctx, solo_db=str(hub))
    assert out["mode"] == "solo" and out["ok"]
    sync.sync(b.ctx, solo_db=str(hub))
    assert {e.thread for e in b.db.evidence()} == {"T1", "T2"}
    assert (hub / "memory.db").is_file()
    t = LocalTransport(Service(ServerDB(hub).init()))
    assert t.meta().body["max_level"] == "episodes"


def test_solo_default_location_is_beside_the_store(tmp_path, clone):
    a = Env(tmp_path, "a", clone).evidence(make_ev("T1"))
    assert sync.sync(a.ctx)["ok"]
    assert (tmp_path / "a" / "memory-server" / "memory.db").is_file()


# ---- CLI ----

def test_cli_token_add_list_revoke_and_the_token_is_shown_once(tmp_path, clone):
    ctx = Env(tmp_path, "a", clone).ctx
    db = str(tmp_path / "srvdb")
    code, out = call(ctx, "memory", "token", "add", "carol", "--db", db)
    assert code == 0 and out["token"].startswith("cck_") and out["role"] == "contributor"
    token = out["token"]
    code, listing = call(ctx, "memory", "token", "list", "--db", db)
    assert token not in json.dumps(listing) and listing["tokens"][0]["name"] == "carol"
    assert ServerDB(tmp_path / "srvdb").authenticate(token) == ("carol", "contributor")
    code, err = call(ctx, "memory", "token", "add", "carol", "--db", db)
    assert code == 1 and "already exists" in err["error"]
    code, out = call(ctx, "memory", "token", "revoke", "carol", "--db", db)
    assert code == 0 and ServerDB(tmp_path / "srvdb").authenticate(token) is None
    assert call(ctx, "memory", "token", "revoke", "nobody", "--db", db)[0] == 1
    assert call(ctx, "memory", "token", "add", "bad name!", "--db", db)[0] == 1


def test_cli_revoke_purge_removes_the_contributors_evidence(tmp_path, clone):
    ctx = Env(tmp_path, "a", clone).ctx
    db = ServerDB(tmp_path / "srvdb").init()
    token = db.add_token("dave")
    s = Service(db, now=lambda: NOW)
    s.put_lessons("dave", {"items": [__import__("test_memory_server").item_for([make_ev("T1")])]})
    assert len(db.evidence()) == 1
    code, out = call(ctx, "memory", "token", "revoke", "dave", "--purge", "--db", str(tmp_path / "srvdb"))
    assert code == 0 and out["purged"]["evidence"] == 1 and db.evidence() == [] and token


def test_cli_serve_refuses_a_public_bind_and_status_never_prints_a_token(tmp_path, clone, srv):
    me = Env(tmp_path, "a", clone, url=url_of(srv), token=srv.alice, share=("o/r", "o/priv"))
    code, out = call(me.ctx, "memory", "serve", "--host", "0.0.0.0", "--db", str(tmp_path / "x"))
    assert code == 1 and "refusing to bind" in out["error"]
    sync.sync(me.ctx)
    code, st = call(me.ctx, "memory", "status")
    assert code == 0
    text = json.dumps(st)
    assert srv.alice not in text and "token" not in st["config"].keys() - {"token_file_set"}
    assert st["server"]["meta"]["max_level"] == "episodes" and st["server"]["mode"] == "server"
    assert [r["level"] for r in st["effective"]] == ["lessons", "lessons"]
    assert st["last_sync"]["ok"] is True and st["store"]["evidence"] == 0
    code, off = call(me.ctx, "memory", "status", "--offline")
    assert off["server"]["meta"] is None and off["effective"][0]["private"] is None


def test_cli_sync_flags(tmp_path, clone, srv):
    me = Env(tmp_path, "a", clone, url=url_of(srv), token=srv.alice).evidence(make_ev("T1"))
    code, out = call(me.ctx, "memory", "sync", "--push-only")
    assert code == 0 and "push" in out and "pull" not in out
    code, out = call(me.ctx, "memory", "sync", "--pull-only")
    assert "pull" in out and "push" not in out
    assert call(me.ctx, "memory", "sync", "--push-only", "--pull-only")[0] == 1


def test_the_default_config_pushes_nothing_and_has_no_network_in_sync(tmp_path, clone):
    me = Env(tmp_path, "a", clone, share=()).evidence(make_ev("T1"))
    out = sync.sync(me.ctx, solo_db=str(tmp_path / "hub"))
    assert out["push"]["pushed"] is False and out["push"]["repos"] == []
    assert "share_repos is empty" in out["push"]["reason"]
    assert out["ok"] is True and out["mode"] == "solo"
    # build_transport opens (and so creates) the hub file even when nothing is pushed, but nothing reaches it.
    assert (tmp_path / "hub" / "memory.db").exists()
    assert ServerDB(tmp_path / "hub").evidence() == []
