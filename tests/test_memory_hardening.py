"""Hardening of the memory server and client: role ceilings, purge propagation, auth throttling, Host rules, bounded push, TLS."""

import http.client
import shutil
import ssl
import subprocess
import threading
import time

import pytest
from test_memory_server import NOW, Srv, item_for, make_ev, put

from codecheck.errors import CodeCheckError
from codecheck.memory.client import HttpTransport, LocalTransport
from codecheck.memory.model import evidence_id
from codecheck.memory.server import AuthLimiter, ServerDB, Service, _source, make_server


@pytest.fixture
def srv(tmp_path):
    s = Srv(tmp_path)
    yield s
    s.close()


# ---- token role ceilings ----

def test_a_contributor_token_cannot_inflate_evidence_to_maintainer(srv):
    status, body = put(srv, srv.carol, [make_ev("T1", role="maintainer"), make_ev("T2", role="maintainer", pr="o/r#2")])
    assert status == 200
    assert body["results"][0]["downgraded_roles"] == 2 and body["downgraded_roles"] == 2
    assert {e.role for e in srv.db.evidence()} == {"contributor"}  # stored, never trusted
    assert not any(le.maintainer for le in srv.db.lessons())  # so a claimed maintainer activates nothing


def test_a_maintainer_ceiling_keeps_the_role_and_other_roles_pass_unchanged(srv):
    evs = [make_ev("T1", role="maintainer"), make_ev("T2", role="author", pr="o/r#2"), make_ev("T3", role="self", pr="o/r#3"),
           make_ev("T4", role="unknown", pr="o/r#4")]
    _, body = put(srv, srv.alice, evs)
    assert body["downgraded_roles"] == 0
    assert {e.thread: e.role for e in srv.db.evidence()} == {"T1": "maintainer", "T2": "author", "T3": "self", "T4": "unknown"}
    _, body = put(srv, srv.carol, [make_ev("T5", role="author", pr="o/r#5"), make_ev("T6", role="self", pr="o/r#6")])
    assert body["downgraded_roles"] == 0  # only a maintainer claim needs permission


def test_a_later_low_ceiling_copy_cannot_overwrite_a_maintainers_role(srv):
    put(srv, srv.alice, [make_ev("T1", role="maintainer")])
    put(srv, srv.carol, [make_ev("T1", role="maintainer")])  # same thread, same id, claimed by the lesser token
    assert srv.db.evidence()[0].role == "maintainer"


def test_a_maintainers_role_leaves_with_the_maintainers_purge(srv):
    put(srv, srv.alice, [make_ev("T1", role="maintainer")])
    put(srv, srv.carol, [make_ev("T1", role="maintainer")])
    srv.db.revoke_token("alice")
    out = srv.db.purge_contributor("alice", srv.service.params, NOW)
    assert out["evidence"] == 0 and out["reweighed"] == 1
    assert srv.db.evidence()[0].role == "contributor"


def test_token_ceilings_are_listed_defaulted_and_migrated(tmp_path):
    db = ServerDB(tmp_path / "s").init()
    db.add_token("a")
    db.add_token("b", ceiling="maintainer")
    assert {t["name"]: t["ceiling"] for t in db.list_tokens()} == {"a": "contributor", "b": "maintainer"}
    with pytest.raises(CodeCheckError):
        db.add_token("c", ceiling="root")
    assert db.ceiling_of("b") == "maintainer" and db.ceiling_of("nobody") == "contributor"
    with db._use(write=True) as conn, db._tx(conn):  # a store made before ceilings existed
        conn.execute("ALTER TABLE tokens DROP COLUMN ceiling")
    again = ServerDB(tmp_path / "s").init()
    assert again.ceiling_of("b") == "contributor"  # the safe default, never an upgrade


def test_solo_mode_keeps_asserting_its_own_roles(tmp_path):
    t = LocalTransport(Service(ServerDB(tmp_path / "s", now=lambda: NOW).init(), now=lambda: NOW))
    resp = t.put_lessons([item_for([make_ev("T1", role="maintainer")])])
    assert resp.status == 200 and resp.body["downgraded_roles"] == 0
    assert t.service.db.evidence()[0].role == "maintainer"


# ---- purge log in pulls (server side) ----

def test_pull_reports_purged_evidence_ids_and_a_watermark(srv):
    put(srv, srv.carol, [make_ev("T1"), make_ev("T2", pr="o/r#2")])
    put(srv, srv.bob, [make_ev("T2", pr="o/r#2")])
    srv.db.revoke_token("carol")
    srv.db.purge_contributor("carol", srv.service.params, NOW)
    _, body = srv.req("GET", "/v1/lessons?repo=o/r&since=0", token=srv.alice)
    t1 = evidence_id("github.com", "o/r", "o/r#1", "T1", "verdict_valid")
    assert body["purged"] == [t1] and body["purged_more"] is False and body["purged_seq"] >= 1  # T2 survives: bob sent it too
    _, again = srv.req("GET", f"/v1/lessons?repo=o/r&since=0&purged_since={body['purged_seq']}", token=srv.alice)
    assert again["purged"] == []
    assert srv.req("GET", "/v1/lessons?repo=x/y&since=0", token=srv.alice)[1]["purged"] == []  # per repository
    assert srv.req("GET", "/v1/lessons?repo=o/r&since=0&purged_since=zz", token=srv.alice)[0] == 422


def test_a_purged_id_that_was_sent_again_is_not_reported(srv):
    put(srv, srv.carol, [make_ev("T1")])
    srv.db.purge_contributor("carol", srv.service.params, NOW)
    put(srv, srv.bob, [make_ev("T1")])
    assert srv.req("GET", "/v1/lessons?repo=o/r&since=0", token=srv.alice)[1]["purged"] == []


def test_the_purge_log_is_bounded_and_a_response_is_capped(srv, monkeypatch):
    from codecheck.memory import server

    monkeypatch.setattr(server, "PURGE_LOG_MAX", 3)
    monkeypatch.setattr(server, "PURGED_PER_RESPONSE", 2)
    put(srv, srv.carol, [make_ev(f"T{i}", pr=f"o/r#{i}") for i in range(5)])
    srv.db.purge_contributor("carol", srv.service.params, NOW)
    with srv.db._use(write=False) as conn:
        assert conn.execute("SELECT count(*) FROM purge_log").fetchone()[0] <= 4  # trimmed to the newest few
    _, body = srv.req("GET", "/v1/lessons?repo=o/r&since=0", token=srv.alice)
    assert len(body["purged"]) == 2 and body["purged_more"] is True
    _, rest = srv.req("GET", f"/v1/lessons?repo=o/r&since=0&purged_since={body['purged_seq']}", token=srv.alice)
    assert rest["purged"] and not set(rest["purged"]) & set(body["purged"])


# ---- failed-token throttle ----

def test_auth_limiter_locks_out_after_n_failures_and_unlocks_with_the_clock():
    t = [100.0]
    lim = AuthLimiter(max_failures=3, window=60, lockout=120, clock=lambda: t[0])
    assert lim.retry_after("1.1.1.1") is None
    for _ in range(3):
        lim.failed("1.1.1.1")
    assert lim.retry_after("1.1.1.1") in (120, 121)
    assert lim.retry_after("2.2.2.2") is None  # another address is not affected
    t[0] += 119
    assert lim.retry_after("1.1.1.1") is not None
    t[0] += 2
    assert lim.retry_after("1.1.1.1") is None
    lim.failed("1.1.1.1")
    assert lim.retry_after("1.1.1.1") is None  # the count restarted


def test_auth_limiter_failures_outside_the_window_do_not_add_up():
    t = [0.0]
    lim = AuthLimiter(max_failures=3, window=60, lockout=60, clock=lambda: t[0])
    for _ in range(5):
        lim.failed("a")
        t[0] += 40
    assert lim.retry_after("a") is None


def test_auth_limiter_table_is_bounded():
    lim = AuthLimiter(max_failures=10, clock=lambda: 0.0)
    for i in range(AuthLimiter.MAX_ADDRESSES + 50):
        lim.failed(f"a{i}")
    assert len(lim._fails) <= AuthLimiter.MAX_ADDRESSES


def _get(port, token=None, **headers):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = dict(headers)
    if token:
        h["Authorization"] = f"Bearer {token}"
    conn.request("GET", "/v1/meta", headers=h)
    r = conn.getresponse()
    body = r.read()
    out = (r.status, r.getheader("Retry-After"), body)
    conn.close()
    return out


def test_the_server_answers_429_with_retry_after_without_revealing_tokens(tmp_path):
    s = Srv(tmp_path, limiter=AuthLimiter(max_failures=3, window=60, lockout=60))
    try:
        assert [_get(s.port, "cck_bad")[0] for _ in range(3)] == [401, 401, 401]
        for tok in (s.alice, "cck_bad", None):  # a valid and an invalid token get the same answer: no oracle
            status, retry, body = _get(s.port, tok)
            assert status == 429 and int(retry) >= 1 and b"valid" not in body
        assert s.req("GET", "/healthz")[0] == 200  # liveness probes are not throttled
    finally:
        s.close()


def test_a_valid_token_from_another_address_is_not_blocked_by_failures(tmp_path):
    s = Srv(tmp_path, limiter=AuthLimiter(max_failures=2, window=60, lockout=60), behind_proxy=True, allow_hosts=["127.0.0.1"])
    try:
        for _ in range(2):
            _get(s.port, "cck_bad", **{"X-Forwarded-For": "6.6.6.6"})
        for xff, want in (("6.6.6.6", 429), ("9.9.9.9", 200), ("6.6.6.6, 9.9.9.9", 200), ("9.9.9.9, 6.6.6.6", 429)):
            assert _get(s.port, s.alice, **{"X-Forwarded-For": xff})[0] == want, xff
        assert _source("1.2.3.4", "6.6.6.6", False) == "1.2.3.4"  # not behind a proxy: the header is ignored
        assert _source("1.2.3.4", "junk", True) == "1.2.3.4"
    finally:
        s.close()


# ---- Host allow-list is mandatory off loopback ----

@pytest.mark.parametrize("kw", [{"behind_proxy": True}, {"tls_cert": "c.pem", "tls_key": "k.pem"}])
def test_a_non_loopback_bind_without_allow_host_is_refused(tmp_path, kw):
    svc = Service(ServerDB(tmp_path / "s").init())
    with pytest.raises(CodeCheckError, match="--allow-host"):
        make_server(svc, "0.0.0.0", 0, **kw)


def test_a_proxy_deployment_checks_the_host_header(tmp_path):
    s = Srv(tmp_path, behind_proxy=True, allow_hosts=["127.0.0.1"])
    try:
        assert s.req("GET", "/v1/meta", token=s.alice)[0] == 200
        assert s.req("GET", "/v1/meta", token=s.alice, headers={"Host": "evil.example"})[0] == 421
    finally:
        s.close()


# ---- TLS with a real certificate ----

@pytest.fixture
def cert(tmp_path):
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("openssl is not installed")
    crt, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    cmd = [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=localhost",
           "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1", "-keyout", str(key), "-out", str(crt)]
    done = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    if done.returncode != 0:
        pytest.skip("this openssl cannot make a self-signed certificate")
    return crt, key


def tls_server(tmp_path, crt, key):
    db = ServerDB(tmp_path / "srv", now=lambda: NOW).init()
    token = db.add_token("alice", ceiling="maintainer")
    server = make_server(Service(db, now=lambda: NOW), "127.0.0.1", 0, tls_cert=str(crt), tls_key=str(key), log=lambda _l: None)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, token


def test_https_works_with_a_custom_ca_and_fails_without_it(tmp_path, cert):
    crt, key = cert
    server, token = tls_server(tmp_path, crt, key)
    try:
        url = f"https://localhost:{server.server_address[1]}"
        ok = HttpTransport(url, token, 5.0, str(crt)).meta()
        assert ok.status == 200 and ok.body["max_level"] == "episodes"
        from codecheck.memory.client import MemoryTransportError

        with pytest.raises(MemoryTransportError) as e:  # the system store does not know this authority
            HttpTransport(url, token, 5.0).meta()
        assert token not in str(e.value)
        with pytest.raises(MemoryTransportError, match="ca_file"):
            HttpTransport(url, token, 5.0, str(tmp_path / "missing.pem")).meta()
        ctx = ssl.create_default_context(cafile=str(crt))  # and the plain stdlib client agrees
        conn = http.client.HTTPSConnection("localhost", server.server_address[1], timeout=5, context=ctx)
        conn.request("GET", "/healthz")
        assert conn.getresponse().status == 200
        conn.close()
    finally:
        server.shutdown()
        server.server_close()


def test_a_plain_http_client_gets_nothing_from_a_tls_server(tmp_path, cert):
    crt, key = cert
    server, _ = tls_server(tmp_path, crt, key)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
        with pytest.raises((http.client.HTTPException, OSError)):
            conn.request("GET", "/healthz")
            conn.getresponse().read()
    finally:
        server.shutdown()
        server.server_close()


def test_ca_file_is_validated():  # it lives in [memory], which a repository config may never set (REPO_FORBIDDEN)
    from codecheck.config import MemoryConfig, validate_memory
    from codecheck.errors import ConfigError

    validate_memory(MemoryConfig(ca_file="~/ca.pem"))
    with pytest.raises(ConfigError, match="ca_file"):
        validate_memory(MemoryConfig(ca_file="-bad"))


# ---- bounded push ----

def _blocking_sync(release: threading.Event, saw: dict):
    def fake_sync(ctx, *, do_pull, http_timeout=None, cancel=None):
        saw["timeout"], saw["cancel"] = http_timeout, cancel
        release.wait(10)
        saw["cancelled_at_end"] = cancel.is_set()
        return {"ok": True}
    return fake_sync


def test_a_hanging_push_is_abandoned_at_the_collection_budget(monkeypatch):
    from types import SimpleNamespace

    from codecheck.memory import autocollect, sync

    release, saw = threading.Event(), {}
    monkeypatch.setattr(sync, "sync", _blocking_sync(release, saw))
    ctx = SimpleNamespace(config=SimpleNamespace(memory=SimpleNamespace(timeout_seconds=1)))
    t = time.monotonic()
    out = autocollect._push_shared(ctx)
    took = time.monotonic() - t
    release.set()
    assert out == {"ok": False, "error": "timeout"} and took < 2.5
    assert saw["timeout"] == 1.0 and saw["cancel"].is_set()  # the abandoned worker is told to write nothing more


def test_a_cancelled_sync_never_writes_sync_json(tmp_path):
    from types import SimpleNamespace

    from codecheck.memory import sync

    ctx = SimpleNamespace(config=SimpleNamespace(memory=SimpleNamespace(store=str(tmp_path / "mem"))))
    ev = threading.Event()
    ev.set()
    sync.save_state(ctx, {"x": 1}, ev)
    assert not (tmp_path / "mem" / sync.STATE_FILE).exists()
    sync.save_state(ctx, {"x": 1})
    assert (tmp_path / "mem" / sync.STATE_FILE).read_text() == '{"x": 1}'
    assert [p.name for p in (tmp_path / "mem").iterdir()] == [sync.STATE_FILE]  # no temp file is left behind
