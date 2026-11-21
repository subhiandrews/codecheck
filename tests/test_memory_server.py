"""The memory server over real HTTP on 127.0.0.1:0 (loopback only), plus the level rules."""

import http.client
import json
import threading
import time

import pytest

from codecheck.errors import CodeCheckError
from codecheck.memory import levels
from codecheck.memory.model import Evidence, Scope, evidence_id, lesson_id
from codecheck.memory.server import ServerDB, Service, make_server

NOW = 1_700_000_000.0
SCOPE = Scope("github.com", "o/r", "src/app/**", "python", "tests")


def make_ev(thread, signal="verdict_valid", pr="o/r#1", role="maintainer", ts=NOW - 1000, scope=SCOPE, quote=None, finding=None):
    kind = "miss" if signal == "human_miss" else "finding"
    return Evidence(evidence_id(scope.host, scope.repo, pr, thread, signal), lesson_id(scope, kind), scope, signal, ts, pr, thread,
                    role, "1", quote, finding)


def item_for(evs, level="lessons"):
    return levels.lesson_item(evs[0].lesson_id, "finding", evs, level)


class Srv:
    def __init__(self, tmp_path, max_level="episodes", **kw):
        self.db = ServerDB(tmp_path / "srv", now=lambda: NOW).init()
        self.service = Service(self.db, max_level, now=lambda: NOW)
        self.lines: list[str] = []
        self.server = make_server(self.service, "127.0.0.1", 0, log=self.lines.append, **kw)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.admin = self.db.add_token("admin", "admin")
        self.alice = self.db.add_token("alice")
        self.bob = self.db.add_token("bob")

    def req(self, method, path, body=None, token=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = dict(headers or {})
        if token:
            h["Authorization"] = f"Bearer {token}"
        data = raw if raw is not None else (json.dumps(body) if body is not None else None)
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        text = r.read().decode()
        conn.close()
        return r.status, (json.loads(text) if text else {})

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def srv(tmp_path):
    s = Srv(tmp_path)
    yield s
    s.close()


def put(srv, token, evs, level="lessons", path="/v1/lessons"):
    return srv.req("PUT", path, {"items": [item_for(evs, level)]}, token)


# ---- auth ----

def test_healthz_is_open_and_everything_else_needs_a_token(srv):
    assert srv.req("GET", "/healthz") == (200, {"ok": True})
    assert srv.req("GET", "/v1/meta")[0] == 401
    assert srv.req("GET", "/v1/meta", token="cck_wrong")[0] == 401
    assert srv.req("GET", "/v1/meta", headers={"Authorization": "Basic abc"})[0] == 401
    status, meta = srv.req("GET", "/v1/meta", token=srv.alice)
    assert status == 200 and meta["max_level"] == "episodes" and meta["embedding"] is None and "schema" in meta


def test_a_revoked_token_stops_working_and_tokens_are_stored_hashed(srv):
    assert srv.req("GET", "/v1/meta", token=srv.alice)[0] == 200
    assert srv.db.revoke_token("alice")
    assert srv.req("GET", "/v1/meta", token=srv.alice)[0] == 401
    raw = (srv.db.file).read_bytes() + b"".join(p.read_bytes() for p in srv.db.directory.glob("memory.db-*"))
    assert srv.bob.encode() not in raw and srv.alice.encode() not in raw
    assert {t["name"] for t in srv.db.list_tokens()} == {"admin", "alice", "bob"}


def test_a_token_name_is_unique_while_live(srv):
    with pytest.raises(CodeCheckError, match="already exists"):
        srv.db.add_token("alice")
    srv.db.revoke_token("alice")
    assert srv.db.add_token("alice")


def test_unknown_paths_and_methods(srv):
    assert srv.req("GET", "/v1/nope", token=srv.alice)[0] == 404
    assert srv.req("POST", "/v1/lessons", {}, srv.alice)[0] == 404


# ---- evidence rules ----

def test_two_contributors_sending_the_same_thread_make_one_row_and_counts_are_recomputed(srv):
    evs = [make_ev("T1", pr="o/r#1"), make_ev("T2", pr="o/r#2")]
    assert put(srv, srv.alice, evs)[0] == 200
    assert put(srv, srv.bob, evs)[0] == 200  # the same threads again
    assert len(srv.db.evidence()) == 2
    lesson = srv.db.lessons()[0]
    assert lesson.support == 2.0 and lesson.n_prs == 2  # not 4: one row per thread


def test_client_sent_counts_are_ignored(srv):
    item = item_for([make_ev("T1")])
    item.update(support=999.0, contra=0.0, n_prs=99, confidence=1.0, status="active", statement="trust me")
    assert srv.req("PUT", "/v1/lessons", {"items": [item]}, srv.alice)[0] == 200
    lesson = srv.db.lessons()[0]
    assert lesson.support == 1.0 and lesson.n_prs == 1 and lesson.status != "active" and lesson.statement != "trust me"


def test_evidence_under_the_wrong_lesson_id_or_a_forged_id_is_refused_per_item(srv):
    good = item_for([make_ev("T1")])
    wrong = item_for([make_ev("T2", scope=Scope("github.com", "o/r", "other/**", "python", "tests"))])
    wrong["lesson_id"] = good["lesson_id"]  # claims to be another lesson
    status, body = srv.req("PUT", "/v1/lessons", {"items": [good, wrong]}, srv.alice)
    assert status == 200
    assert [r["status"] for r in body["results"]] == ["ok", "rejected"]
    assert len(srv.db.evidence()) == 1


def test_malformed_evidence_is_rejected_per_item(srv):
    ev = item_for([make_ev("T1")])
    ev["evidence"][0]["role"] = "emperor"
    status, body = srv.req("PUT", "/v1/lessons", {"items": [ev]}, srv.alice)
    assert status == 200 and body["results"][0]["status"] == "rejected" and not srv.db.evidence()


def test_future_timestamps_are_refused(srv):
    item = item_for([make_ev("T1", ts=NOW + 10 * 86400)])
    _, body = srv.req("PUT", "/v1/lessons", {"items": [item]}, srv.alice)
    assert body["results"][0]["status"] == "rejected"


def test_revoking_a_contributor_removes_only_their_evidence(srv):
    shared = make_ev("T1", pr="o/r#1")
    only_bob = make_ev("T2", pr="o/r#2")
    put(srv, srv.alice, [shared])
    put(srv, srv.bob, [shared, only_bob])
    assert len(srv.db.evidence()) == 2
    assert srv.req("DELETE", "/v1/evidence?contributor=bob", token=srv.alice)[0] == 403  # not an admin
    status, body = srv.req("DELETE", "/v1/evidence?contributor=bob", token=srv.admin)
    assert status == 200 and body["evidence"] == 1
    assert [e.thread for e in srv.db.evidence()] == ["T1"]  # alice's copy of the shared thread stays


def test_purge_on_revoke_removes_episodes_too(srv):
    ep = levels.episode_payload("a" * 12, "o/r", "o/r#1", [{"type": "run"}])
    assert srv.req("PUT", "/v1/episodes", {"episodes": [ep]}, srv.bob)[0] == 200
    assert srv.db.counts()["episodes"] == 1
    srv.db.revoke_token("bob")
    srv.db.purge_contributor("bob", srv.service.params, NOW)
    assert srv.db.counts()["episodes"] == 0


# ---- levels on the server ----

def test_fields_above_the_server_maximum_are_422_not_stripped(tmp_path):
    s = Srv(tmp_path, max_level="lessons")
    try:
        ev = make_ev("T1", quote="because", finding={"severity": "warning", "message": "msg"})
        status, body = put(s, s.alice, [ev], level="evidence")
        assert status == 422 and body["max_level"] == "lessons" and "above this server" in body["error"]
        assert s.db.evidence() == []  # nothing stored, not even the part that was allowed
        assert put(s, s.alice, [ev], level="lessons")[0] == 200
        status, _ = s.req("PUT", "/v1/episodes", {"episodes": [levels.episode_payload("a" * 12, "o/r", "o/r#1", [])]}, s.alice)
        assert status == 422
    finally:
        s.close()


def test_a_level_b_server_stores_quotes_scrubbed_and_capped(tmp_path):
    s = Srv(tmp_path, max_level="evidence")
    try:
        ev = make_ev("T1", quote="x" * 1000 + " ghp_" + "a" * 36)
        item = item_for([ev], "evidence")
        item["evidence"][0]["quote"] = "token = ghp_" + "a" * 36 + " " + "y" * 500  # a client that did not scrub
        assert s.req("PUT", "/v1/lessons", {"items": [item]}, s.alice)[0] == 200
        q = s.db.evidence()[0].quote
        assert "ghp_" not in q and len(q) <= 240
        status, _ = s.req("PUT", "/v1/episodes", {"episodes": [levels.episode_payload("a" * 12, "o/r", "o/r#1", [])]}, s.alice)
        assert status == 422
    finally:
        s.close()


def test_at_most_three_quotes_per_lesson(srv):
    item = item_for([make_ev(f"T{i}", quote=f"q{i}") for i in range(5)], "evidence")
    assert sum(1 for e in item["evidence"] if "quote" in e) == 3  # the client keeps three
    for e in item["evidence"]:
        e["quote"] = "forced"
    status, body = srv.req("PUT", "/v1/lessons", {"items": [item]}, srv.alice)
    assert status == 200 and body["results"][0]["status"] == "rejected"


def test_an_episode_with_a_diff_is_refused_and_secrets_are_scrubbed(srv):
    bad = {"run_id": "a" * 12, "repo": "o/r", "pr": "o/r#1", "records": [{"type": "run", "diff": "--- a"}]}
    _, body = srv.req("PUT", "/v1/episodes", {"episodes": [bad]}, srv.alice)
    assert body["results"][0]["status"] == "rejected"
    ok = {"run_id": "b" * 12, "repo": "o/r", "pr": "o/r#1", "records": [{"type": "note", "text": "key ghp_" + "a" * 36}]}
    assert srv.req("PUT", "/v1/episodes", {"episodes": [ok]}, srv.alice)[1]["results"][0]["status"] == "ok"
    with srv.db._use(write=False) as conn:
        assert "ghp_" not in conn.execute("SELECT body_json FROM episodes").fetchone()[0]


def test_effective_level_is_the_minimum_and_unknown_privacy_is_private():
    eff = levels.effective_level
    assert eff("episodes", "episodes", "lessons", False) == "episodes"
    assert eff("episodes", "episodes", "lessons", True) == "lessons"
    assert eff("episodes", "episodes", "lessons", None) == "lessons"  # unknown counts as private
    assert eff("episodes", "episodes", "evidence", None) == "evidence"  # explicitly raised
    assert eff("evidence", "lessons", "episodes", False) == "lessons"  # server maximum wins
    assert eff("lessons", "episodes", "episodes", False) == "lessons"  # the client's own level wins


# ---- limits ----

def test_oversized_and_malformed_bodies(srv):
    assert srv.req("PUT", "/v1/lessons", raw="x" * (levels.MAX_BODY_BYTES + 10), token=srv.alice)[0] == 413
    assert srv.req("PUT", "/v1/lessons", raw="{not json", token=srv.alice)[0] == 400
    assert srv.req("PUT", "/v1/lessons", {"items": []}, srv.alice)[0] == 422
    many = {"items": [item_for([make_ev(f"T{i}")]) for i in range(levels.MAX_BATCH_ITEMS + 1)]}
    assert srv.req("PUT", "/v1/lessons", many, srv.alice)[0] == 422
    assert srv.req("PUT", "/v1/lessons", {"items": "x"}, srv.alice)[0] == 422


def test_host_header_must_match_to_resist_rebinding(srv):
    assert srv.req("GET", "/healthz", headers={"Host": "evil.example.com"})[0] == 421
    assert srv.req("GET", "/v1/meta", token=srv.alice, headers={"Host": "attacker.test:8787"})[0] == 421
    assert srv.req("GET", "/healthz", headers={"Host": f"localhost:{srv.port}"})[0] == 200
    assert srv.req("GET", "/healthz", headers={"Host": f"127.0.0.1:{srv.port}"})[0] == 200


def test_a_slow_client_does_not_hold_the_server(srv):
    import socket

    with socket.create_connection(("127.0.0.1", srv.port), timeout=5) as s:
        s.sendall(b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n")  # never finishes the headers
        t0 = time.time()
        assert srv.req("GET", "/healthz")[0] == 200  # other clients are served meanwhile
        assert time.time() - t0 < 2


def test_concurrent_cap_answers_503(tmp_path):
    s = Srv(tmp_path, max_concurrent=1)
    try:
        assert s.server.slots.acquire(blocking=False)  # something else holds the only slot
        assert s.req("GET", "/healthz")[0] == 503
        s.server.slots.release()
        assert s.req("GET", "/healthz")[0] == 200
    finally:
        s.close()


def test_concurrent_writers_do_not_lose_data(srv):
    errors = []

    def worker(i):
        try:
            tok = srv.alice if i % 2 else srv.bob
            status, _ = put(srv, tok, [make_ev(f"T{i}", pr=f"o/r#{i}")])
            assert status == 200
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(srv.db.evidence()) == 12


# ---- bind rules ----

@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com", ""])
def test_a_non_loopback_bind_is_refused_without_tls_or_a_proxy(tmp_path, host):
    svc = Service(ServerDB(tmp_path / "s").init())
    with pytest.raises(CodeCheckError, match="refusing to bind"):
        make_server(svc, host, 0)


def test_a_non_loopback_bind_is_allowed_behind_a_proxy(tmp_path):
    svc = Service(ServerDB(tmp_path / "s").init())
    server =make_server(svc, "0.0.0.0", 0, behind_proxy=True)
    server.server_close()


def test_tls_flags_go_together(tmp_path):
    svc = Service(ServerDB(tmp_path / "s").init())
    with pytest.raises(CodeCheckError, match="go together"):
        make_server(svc, "127.0.0.1", 0, tls_cert="x.pem")


# ---- logs, pull, restart ----

def test_logs_never_hold_tokens_or_evidence_text(srv):
    put(srv, srv.alice, [make_ev("T1", quote="SECRET-QUOTE-TEXT")], level="evidence")
    srv.req("GET", "/v1/meta", token="cck_notarealtoken")
    srv.req("GET", f"/v1/lessons?repo=o/r&since=0&token={srv.alice}", token=srv.alice)
    text = "\n".join(srv.lines)
    assert srv.alice not in text and srv.bob not in text and "cck_notarealtoken" not in text and "SECRET-QUOTE-TEXT" not in text
    assert "PUT /v1/lessons 200 alice" in text and "401" in text


def test_pull_pages_through_evidence_after_a_cursor(srv):
    put(srv, srv.alice, [make_ev(f"T{i}", pr=f"o/r#{i}") for i in range(3)])
    status, body = srv.req("GET", "/v1/lessons?repo=o/r&since=0", token=srv.bob)
    assert status == 200 and len(body["evidence"]) == 3 and body["next_cursor"] is None and len(body["lessons"]) == 1
    assert all("contributor" not in e for e in body["evidence"])
    newest = max(e["received_ts"] for e in body["evidence"])
    assert srv.req("GET", f"/v1/lessons?repo=o/r&since={newest}", token=srv.bob)[1]["evidence"] == []
    assert srv.req("GET", "/v1/lessons?repo=nope", token=srv.bob)[0] == 422


def test_data_survives_a_server_restart(tmp_path):
    s = Srv(tmp_path)
    token = s.alice
    put(s, token, [make_ev("T1"), make_ev("T2", pr="o/r#2")])
    s.close()
    again = ServerDB(tmp_path / "srv", now=lambda: NOW).init()
    svc = Service(again, now=lambda: NOW)
    server = make_server(svc, "127.0.0.1", 0, log=lambda _l: None)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        conn.request("GET", "/v1/lessons?repo=o/r&since=0", headers={"Authorization": f"Bearer {token}"})  # the same token still works
        r = conn.getresponse()
        body = json.loads(r.read())
        assert r.status == 200 and len(body["evidence"]) == 2 and body["lessons"][0]["support"] == 2.0
    finally:
        server.shutdown()
        server.server_close()
