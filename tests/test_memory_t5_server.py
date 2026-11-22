"""Review fixes: no environment proxy for memory HTTP, a reset server store is a purge gap, a failed purge is retryable."""

import http.server
import socket
import threading

import pytest
from clihelp import call
from gitutil import make_origin_and_clone
from test_memory_server import NOW, Srv, item_for, make_ev
from test_memory_sync import Env

from codecheck.memory.client import HttpTransport
from codecheck.memory.embed import OllamaEmbedder
from codecheck.memory.server import ServerDB, Service


@pytest.fixture
def srv(tmp_path):
    s = Srv(tmp_path)
    yield s
    s.close()


@pytest.fixture
def proxy(monkeypatch):
    """A listener standing in for HTTP_PROXY: it records whether anything ever connects."""
    lst = socket.socket()
    lst.bind(("127.0.0.1", 0))
    lst.listen(5)
    hits = []
    stop = threading.Event()

    def accept():
        lst.settimeout(0.1)
        while not stop.is_set():
            try:
                c, _ = lst.accept()
            except OSError:
                continue
            hits.append(1)
            c.close()

    t = threading.Thread(target=accept, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{lst.getsockname()[1]}"
    for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(k, url)
    for k in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(k, raising=False)
    yield hits
    stop.set()
    t.join(2)
    lst.close()


def test_the_memory_client_never_sends_its_token_through_an_environment_proxy(srv, proxy):
    t = HttpTransport(f"http://127.0.0.1:{srv.port}", srv.alice)
    assert t.get_lessons("o/r").status == 200
    assert proxy == []


def test_the_ollama_embedder_never_uses_an_environment_proxy(proxy):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"models": []}')

        def log_message(self, *a):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        emb = OllamaEmbedder(f"http://127.0.0.1:{httpd.server_address[1]}", "m")
        assert emb._call("GET", "/api/tags", None, 3.0) == {"models": []}
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert proxy == []


def test_a_client_ahead_of_a_recreated_server_store_sees_a_purged_gap(srv):
    srv.db.purge_contributor("carol", srv.service.params, NOW)  # the log holds at most id 1; the client below holds 500
    status, body = srv.service.get_lessons({"repo": ["o/r"], "purged_since": ["500"]})
    assert status == 200 and body["purged_gap"] is True
    status, body = srv.service.get_lessons({"repo": ["o/r"], "purged_since": ["0"]})
    assert body["purged_gap"] is False
    status, body = srv.service.get_lessons({"repo": ["o/r"], "purged_since": [str(srv.db.purge_seq())]})
    assert body["purged_gap"] is False


def test_a_failed_revoke_purge_can_be_retried(tmp_path, monkeypatch):
    clone = make_origin_and_clone(tmp_path)[1]
    ctx = Env(tmp_path, "a", clone).ctx
    db = ServerDB(tmp_path / "srvdb").init()
    db.add_token("dave")
    Service(db, now=lambda: NOW).put_lessons("dave", {"items": [item_for([make_ev("T1")])]})
    path = str(tmp_path / "srvdb")
    real = ServerDB.purge_contributor
    calls = []

    def flaky(self, *a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("disk full")
        return real(self, *a, **k)

    monkeypatch.setattr(ServerDB, "purge_contributor", flaky)
    assert call(ctx, "memory", "token", "revoke", "dave", "--purge", "--db", path)[0] == 1
    assert len(db.evidence()) == 1
    code, out = call(ctx, "memory", "token", "revoke", "dave", "--purge", "--db", path)
    assert code == 0 and out["purged"]["evidence"] == 1 and db.evidence() == []
    # without --purge a revoked name is still an error
    assert call(ctx, "memory", "token", "revoke", "dave", "--db", path)[0] == 1
