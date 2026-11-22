"""Phase 5: semantic retrieval with an embedder, on a fake Ollama (loopback only) and an injected fake embedder."""

import copy
import hashlib
import http.client
import json
import re
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from clihelp import call, make_ctx
from fakes import FakeProvider
from gitutil import make_origin_and_clone
from test_memory_read import context, lesson, local_ctx, store
from test_memory_server import NOW, Srv, item_for, make_ev

from codecheck.config import MemoryConfig, load_config
from codecheck.errors import ConfigError
from codecheck.memory import embed, levels, semantic, sync
from codecheck.memory.client import HttpTransport, LocalTransport
from codecheck.memory.db import MemoryDB
from codecheck.memory.embed import OllamaEmbedder, Pin
from codecheck.memory.model import Evidence, Scope, evidence_id, lesson_id
from codecheck.memory.retrieve import merge_ranked
from codecheck.memory.server import ServerDB, Service

pytestmark = pytest.mark.usefixtures("git_env")

DIM = 256
DIGEST = "sha256:aaaa"


def words(text):
    return re.findall(r"[a-z0-9]+", text.lower())


class FakeEmbedder:
    """Bag of words hashed into DIM buckets: texts sharing words are similar. No network."""

    def __init__(self, model="fake-embed", digest=DIGEST, fail=False, dim=DIM):
        self.model, self._digest, self.fail, self.dim = model, digest, fail, dim
        self.calls: list[list[str]] = []

    def digest(self):
        return None if self.fail else self._digest

    def embed(self, texts, budget=5.0):
        if self.fail:
            return None
        self.calls.append(list(texts))
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for w in words(t):
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % self.dim] += 1.0
            out.append(v)
        return out


@pytest.fixture
def fake(monkeypatch):
    emb = FakeEmbedder()
    monkeypatch.setattr(semantic, "make_embedder", lambda cfg: emb if cfg.embed_model else None)
    return emb


# ---------- the Ollama embedder against a fake Ollama ----------

class FakeOllama:
    def __init__(self, mode="ok"):
        self.mode, self.requests = mode, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                return

            def _json(self, body):
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _go(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                outer.requests.append((self.command, self.path, body))
                mode = outer.mode
                if mode == "slow":
                    time.sleep(1.5)
                if mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:1/")
                    self.end_headers()
                    return
                if mode == "drop":
                    self.connection.close()
                    return
                if mode == "malformed":
                    self.send_response(200)
                    self.send_header("Content-Length", "5")
                    self.end_headers()
                    self.wfile.write(b"{nope")
                    return
                if mode == "error":
                    self.send_response(500)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/api/tags":
                    self._json({"models": [{"name": "m:latest", "digest": "sha256:abc"}, {"name": "other", "digest": "x"}]})
                elif mode == "short":
                    self._json({"embeddings": [[1.0, 0.0]]})
                elif mode == "huge":
                    self._json({"embeddings": [[1.0] * 10 for _ in body["input"]], "pad": "x" * 2000})
                else:
                    self._json({"embeddings": [[float(len(t)), 1.0, 0.0] for t in body["input"]]})

            do_GET = do_POST = _go

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def ollama():
    made = []

    def make(mode="ok"):
        o = FakeOllama(mode)
        made.append(o)
        return o

    yield make
    for o in made:
        o.close()


def test_the_embedder_batches_and_reads_the_digest(ollama):
    o = ollama()
    e = OllamaEmbedder(o.url, "m")
    vecs = e.embed([f"t{i}" for i in range(70)])
    assert vecs is not None and len(vecs) == 70 and len(vecs[0]) == 3
    posts = [r for r in o.requests if r[0] == "POST"]
    assert [len(r[2]["input"]) for r in posts] == [32, 32, 6] and posts[0][1] == "/api/embed" and posts[0][2]["model"] == "m"
    assert e.digest() == "sha256:abc"
    assert OllamaEmbedder(o.url, "absent").digest() is None


@pytest.mark.parametrize("mode", ["malformed", "drop", "redirect", "error"])
def test_every_failure_gives_none_and_never_raises(ollama, mode):
    o = ollama(mode)
    e = OllamaEmbedder(o.url, "m")
    assert e.embed(["a", "b"]) is None
    assert e.digest() is None


def test_a_wrong_number_of_vectors_is_refused(ollama):
    assert OllamaEmbedder(ollama("short").url, "m").embed(["a", "b"]) is None


def test_the_embedder_keeps_to_its_time_budget(ollama):
    o = ollama("slow")
    started = time.monotonic()
    assert OllamaEmbedder(o.url, "m", budget=0.3).embed(["a"]) is None
    assert time.monotonic() - started < 1.2


def test_an_oversized_answer_is_refused(ollama, monkeypatch):
    monkeypatch.setattr(embed, "RESPONSE_CAP", 500)
    assert OllamaEmbedder(ollama("huge").url, "m").embed(["a"]) is None


def test_plain_http_to_another_machine_is_refused_without_a_connection(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("a socket was opened")

    monkeypatch.setattr(http.client.HTTPConnection, "connect", boom)
    for url in ("http://example.com:11434", "http://10.0.0.5:11434", "https://user:pw@example.com", "ftp://x"):
        assert OllamaEmbedder(url, "m").embed(["a"]) is None
        assert OllamaEmbedder(url, "m").digest() is None


def test_an_http_exception_is_caught(monkeypatch, ollama):
    def raise_it(*_a, **_k):
        raise http.client.BadStatusLine("x")

    monkeypatch.setattr("urllib.request.OpenerDirector.open", raise_it)
    assert OllamaEmbedder(ollama().url, "m").embed(["a"]) is None


# ---------- vectors ----------

def test_a_vector_blob_roundtrips_and_truncates():
    v = [3.0, 4.0, 0.0, 12.0]
    blob = embed.pack(v)
    back = embed.unpack(blob)
    assert len(blob) == 16 and abs(sum(x * x for x in back) - 1.0) < 1e-6
    cut = embed.unpack(embed.pack(v, 2))
    assert len(cut) == 2 and abs(cut[0] - 0.6) < 1e-6 and abs(cut[1] - 0.8) < 1e-6
    assert embed.unpack(b"123") is None
    assert embed.from_b64(embed.to_b64(blob), 4) == blob
    assert embed.from_b64(embed.to_b64(blob), 5) is None and embed.from_b64("!!!", 4) is None
    assert abs(embed.cosine([1, 0], [1, 1]) - 0.7071) < 1e-3


def test_what_is_embedded_is_the_evidence_text_not_the_statement():
    s = Scope("github.com", "o/r", "src/app/**", "python", "tests")
    with_finding = Evidence("e1", "l", s, "verdict_valid", NOW, "o/r#1", "t", "maintainer", "1", None, {"severity": "warning", "message": "null deref"})
    miss = Evidence("e2", "l", s, "human_miss", NOW, "o/r#1", "t2", "maintainer", "1", "please check the null", None)
    bare = Evidence("e3", "l", s, "verdict_valid", NOW, "o/r#1", "t3", "maintainer", "1", None, None)
    assert semantic.evidence_text(with_finding) == "null deref (outcome: verdict_valid)"
    assert semantic.evidence_text(miss) == "please check the null"
    assert semantic.evidence_text(bare) is None


def seeded(tmp_path, n=1, text="user input is not checked for null before strip", repo="o/r", signal="human_miss", **over):
    s = Scope("github.com", repo, "src/app/**", "go", "any" if signal == "human_miss" else "security")
    kind = "miss" if signal == "human_miss" else "finding"
    db = MemoryDB(tmp_path / "mem", now=lambda: NOW)
    evs = []
    for i in range(n):
        evs.append(Evidence(evidence_id("github.com", repo, f"{repo}#{i}", f"t{i}", signal), lesson_id(s, kind), s, signal, NOW - i, f"{repo}#{i}",
                            f"t{i}", "maintainer", "1", text if signal == "human_miss" else None,
                            {"severity": "warning", "message": text} if signal != "human_miss" else None))
    db.upsert_evidence(evs)
    return db, s, kind


def cfg_for(tmp_path, **over):
    return MemoryConfig(enabled=True, read=True, reviewers=True, store=str(tmp_path / "mem"), embed_model="fake-embed",
                        min_similarity=0.3, **over)


def test_refresh_embeds_evidence_with_text_only(tmp_path, fake):
    db, s, _ = seeded(tmp_path, 3)
    bare = Evidence("zz", lesson_id(s, "finding"), s, "verdict_valid", NOW, "o/r#9", "tz", "maintainer", "1", None, None)
    db.upsert_evidence([bare])
    ctx = make_ctx(tmp_path, tmp_path, memory=cfg_for(tmp_path))
    out = semantic.refresh_local(ctx)
    assert out["embedded"] == 3 and out["skipped_no_text"] == 1 and out["remaining"] == 0 and out["dim"] == DIM
    assert semantic.vector_stats(db)["vectors"] == 3
    again = semantic.refresh_local(ctx)
    assert again["embedded"] == 0
    assert semantic.refresh_local(ctx, rebuild=True)["embedded"] == 3


def test_refresh_without_a_model_or_an_embedder_does_nothing_and_never_raises(tmp_path, monkeypatch, fake):
    seeded(tmp_path)
    off = make_ctx(tmp_path, tmp_path, memory=replace(cfg_for(tmp_path), embed_model=""))
    assert semantic.refresh_local(off)["embedded"] == 0 and "off" in semantic.refresh_local(off)["reason"]
    fake.fail = True
    on = make_ctx(tmp_path, tmp_path, memory=cfg_for(tmp_path))
    out = semantic.refresh_local(on)
    assert out["embedded"] == 0 and "unreachable" in out["reason"]


def test_ranking_orders_by_similarity_applies_the_threshold_and_groups_by_lesson(tmp_path, fake):
    db, s, _kind = seeded(tmp_path, 2, text="user input is not checked for null before strip")
    _other, _, _ = seeded(tmp_path, 1, text="database migration lacks a rollback step", repo="o/r")
    # a second lesson (other scope) with unrelated text
    s2 = Scope("github.com", "o/r", "db/**", "sql", "any")
    ev2 = Evidence(evidence_id("github.com", "o/r", "o/r#50", "tq", "human_miss"), lesson_id(s2, "miss"), s2, "human_miss", NOW, "o/r#50", "tq",
                   "maintainer", "1", "database migration lacks a rollback step", None)
    db.upsert_evidence([ev2])
    ctx = make_ctx(tmp_path, tmp_path, memory=cfg_for(tmp_path))
    semantic.refresh_local(ctx)
    qs = [semantic.Query("def f(user): return user.input.strip()  # null unchecked")]
    ranked = semantic.rank_lessons(db, "o/r", qs, fake, min_similarity=0.3, budget=2.0)
    assert [lid for lid, _ in ranked] == [lesson_id(s, "miss")]  # two evidence items collapse into one lesson
    assert ranked[0][1] > 0.3
    everything = semantic.rank_lessons(db, "o/r", qs, fake, min_similarity=-1.0, budget=2.0)
    assert [lid for lid, _ in everything] == [lesson_id(s, "miss"), lesson_id(s2, "miss")]
    assert semantic.rank_lessons(db, "o/r", qs, fake, min_similarity=0.99, budget=2.0) == []
    assert semantic.rank_lessons(db, "other/repo", qs, fake, min_similarity=0.0, budget=2.0) == []  # prefiltered by repo


def test_the_candidate_cap_and_the_query_cap(tmp_path, fake):
    db, _s, _ = seeded(tmp_path, embed.MAX_CANDIDATES + 5)
    semantic.refresh_local(make_ctx(tmp_path, tmp_path, memory=cfg_for(tmp_path)), budget=60)
    assert len(semantic.candidates(db, "o/r", "fake-embed", DIGEST)) == embed.MAX_CANDIDATES
    many = [semantic.Query(f"query number {i} " + "x" * 600) for i in range(100)]
    cleaned = semantic.clean_queries(many)
    assert len(cleaned) == embed.MAX_QUERIES and all(len(q.text) <= embed.QUERY_CAP for q in cleaned)
    assert semantic.clean_queries([semantic.Query("key ghp_" + "a" * 36)])[0].text.count("ghp_a") == 0  # scrubbed


def test_a_model_change_never_mixes_vectors(tmp_path, fake):
    db, _s, _ = seeded(tmp_path)
    ctx = make_ctx(tmp_path, tmp_path, memory=cfg_for(tmp_path))
    semantic.refresh_local(ctx)
    fake._digest = "sha256:new"
    qs = [semantic.Query("user input null strip")]
    assert semantic.rank_lessons(db, "o/r", qs, fake, min_similarity=0.0, budget=2.0) == []  # old vectors are another space
    assert semantic.refresh_local(ctx)["embedded"] == 1
    assert len(semantic.rank_lessons(db, "o/r", qs, fake, min_similarity=0.0, budget=2.0)) == 1


# ---------- the review path ----------

def write_b(ctx):
    root = Path(ctx.cwd)
    (root / "src" / "app" / "b.py").write_text("def fetch(user):\n    return user.input.strip()  # unchecked null input\n")


def go_lesson(statement, text, tmp_path, status="active", glob="src/app/**", language="go"):
    """A miss lesson whose language (go) does not match the python diff: exact-key lookup cannot find it."""
    le = lesson(kind="miss", statement=statement, language=language, glob=glob, status=status)
    ev = Evidence(evidence_id("github.com", "o/r", "o/r#7", "t" + statement[:4], "human_miss"), le.id, le.scope, "human_miss", NOW, "o/r#7",
                  "t" + statement[:4], "maintainer", "1", text, None)
    db = store(tmp_path, le)
    db.upsert_evidence([ev])
    return le


def test_a_lesson_found_by_similarity_that_exact_key_misses(tmp_path, fake):
    le = go_lesson("check inputs for null", "user input is not checked for null before strip", tmp_path)
    go_lesson("always add a rollback", "database migration lacks a rollback step", tmp_path, language="ruby")
    ctx = local_ctx(tmp_path, memory=cfg_for(tmp_path))
    semantic.refresh_local(ctx)
    write_b(ctx)
    out, meta = context(ctx)
    assert meta["memory"]["read"] is True and meta["memory"]["ids"] == [le.id]  # only the similar one
    text = (Path(out["run_dir"]) / "memory.md").read_text()
    assert "user input is not checked for null before strip" in text and "rollback" not in text
    # the same review with embeddings off finds nothing: exact-key needs the python language
    (tmp_path / "off").mkdir()
    off = local_ctx(tmp_path / "off", memory=replace(cfg_for(tmp_path), embed_model=""))
    write_b(off)
    _o, m2 = context(off)
    assert m2["memory"] == {"read": False, "reason": "no lessons"}


def test_candidates_and_expired_lessons_are_never_shown_by_similarity(tmp_path, fake):
    go_lesson("candidate lesson", "user input is not checked for null before strip", tmp_path, status="candidate")
    ctx = local_ctx(tmp_path, memory=cfg_for(tmp_path))
    semantic.refresh_local(ctx)
    write_b(ctx)
    _out, meta = context(ctx)
    assert meta["memory"] == {"read": False, "reason": "no lessons", "semantic": "ok"}


def test_exact_hits_come_first_and_the_cap_holds(tmp_path):
    exact = lesson(kind="miss", statement="exact", language="python")
    sim = [lesson(kind="miss", statement=f"sim{i}", language="go", glob="src/app/**") for i in range(5)]
    sim = [replace(x, id=f"{i:016x}") for i, x in enumerate(sim)]
    db = store(tmp_path, exact, *sim)
    out = merge_ranked(db, "o/r", "miss", [exact], [(x.id, 0.9 - i / 10) for i, x in enumerate(sim)], ["src/app/a.py"], 3)
    assert [x.statement for x in out] == ["exact", "sim0", "sim1"]
    assert merge_ranked(db, "o/r", "miss", [exact], [(sim[0].id, 0.9)], ["src/app/a.py"], 1) == [exact]
    assert merge_ranked(db, "o/r", "miss", [], [("deadbeefdeadbeef", 0.9)], ["src/app/a.py"], 3) == []  # an unknown id is skipped


def test_the_aggregator_stage_uses_the_finding_messages(tmp_path, fake):
    _seeded_db, s, _ = seeded(tmp_path, 3, text="unchecked null input in strip call", signal="verdict_valid")
    le = Lesson_for(s)
    db = store(tmp_path, le)
    ctx = local_ctx(tmp_path, memory=cfg_for(tmp_path))
    semantic.refresh_local(ctx)
    write_b(ctx)
    out, meta = context(ctx)
    run = out["run_id"]
    from clihelp import write_findings

    write_findings(Path(out["run_dir"]), {m: [] for m in meta["panel"]["members"]})
    first = meta["panel"]["members"][0]
    write_findings(Path(out["run_dir"]), {first: [{"file": "src/app/b.py", "line": 2, "severity": "warning", "category": "style",
                                                    "message": "unchecked null input reaches the strip call"}]})
    code, res = call(ctx, "ingest", "--run", run)
    assert code == 0 and res["memory"]["read"] is True and res["memory"]["lessons"] == 1, res
    assert db.lesson(le.id) is not None


def Lesson_for(scope):
    return lesson(kind="finding", statement="null inputs are real", language=scope.language, glob=scope.path_glob, category=scope.category,
                  lean="confirm")


def test_default_output_is_unchanged_without_a_model(tmp_path):
    le = go_lesson("exact only", "anything", tmp_path)
    ctx = local_ctx(tmp_path, memory=replace(cfg_for(tmp_path), embed_model=""))
    write_b(ctx)
    out, meta = context(ctx)
    assert meta["memory"]["reason"] == "no lessons" and not (Path(out["run_dir"]) / "memory.md").exists()
    assert le.id and semantic.vector_stats(MemoryDB(tmp_path / "mem"))["vectors"] == 0  # no table, nothing computed


def test_retrieval_never_raises_and_respects_the_time_budget(tmp_path, monkeypatch):
    go_lesson("slow lesson", "user input is not checked for null before strip", tmp_path)

    class Slow(FakeEmbedder):
        def digest(self):
            time.sleep(5)
            return DIGEST

    monkeypatch.setattr(semantic, "make_embedder", lambda cfg: Slow())
    ctx = local_ctx(tmp_path, memory=replace(cfg_for(tmp_path), timeout_seconds=1))
    write_b(ctx)
    started = time.monotonic()
    _out, meta = context(ctx)
    assert time.monotonic() - started < 4.0
    assert meta["memory"]["reason"] == "no lessons"  # exact-key result, the review went on

    def explode(cfg):
        raise RuntimeError("boom")

    monkeypatch.setattr(semantic, "make_embedder", explode)
    assert semantic.ranked_for(ctx, MemoryDB(tmp_path / "mem"), "o/r", [semantic.Query("x")], "miss") == []


# ---------- the server: vectors, the pin and the 409 ----------

@pytest.fixture
def srv(tmp_path):
    s = Srv(tmp_path / "s")
    yield s
    s.close()


def with_vectors(item, evs, emb, pin_digest=DIGEST, model="fake-embed", dim=DIM):
    ordered = sorted(evs, key=lambda e: (e.ts, e.id))
    for row, e in zip(item["evidence"], ordered, strict=True):
        vec = embed.pack(emb.embed([semantic.evidence_text(e) or "x"])[0], dim)
        row["vector"] = {"model": model, "digest": pin_digest, "dim": dim, "b64": embed.to_b64(vec)}
    return item


def finding_ev(thread="a", msg="user input is not checked for null", **kw):
    return make_ev(thread, finding={"severity": "warning", "message": msg}, **kw)


def test_the_first_accepted_vector_pins_the_server_and_a_mismatch_is_409(srv):
    emb = FakeEmbedder()
    ev = finding_ev()
    item = with_vectors(item_for([ev], "evidence"), [ev], emb)
    status, body = srv.req("PUT", "/v1/lessons", {"items": [item]}, srv.alice)
    assert status == 200 and body["results"][0]["status"] == "ok"
    _, meta = srv.req("GET", "/v1/meta", token=srv.alice)
    assert meta["embedding"] == {"model": "fake-embed", "digest": DIGEST, "dim": DIM, "search": False}
    assert semantic.vector_stats(srv.db)["vectors"] == 1
    ev2 = finding_ev("b")
    for bad in ({"digest": "sha256:other"}, {"model": "else"}, {"dim": 8}):
        item2 = with_vectors(item_for([ev2], "evidence"), [ev2], emb)
        item2["evidence"][0]["vector"].update(bad)
        if "dim" in bad:
            item2["evidence"][0]["vector"]["b64"] = embed.to_b64(embed.pack([1.0] * 8))
        status, body = srv.req("PUT", "/v1/lessons", {"items": [item2]}, srv.alice)
        assert status == 409 and body["embedding"]["digest"] == DIGEST
    assert semantic.vector_stats(srv.db)["vectors"] == 1  # nothing from a refused request was stored
    assert srv.db.counts()["evidence"] == 1


def test_a_vector_is_refused_where_no_text_was_shared(tmp_path):
    emb = FakeEmbedder()
    ev = finding_ev()
    item = with_vectors(item_for([ev], "evidence"), [ev], emb)
    for level in ("lessons",):
        s = Srv(tmp_path / level, max_level=level)
        try:
            status, body = s.req("PUT", "/v1/lessons", {"items": [item]}, s.alice)
            assert status == 422 and "maximum sharing level" in body["error"] and s.db.counts()["evidence"] == 0
            bare = item_for([make_ev("n")], "lessons")
            bare["evidence"][0]["vector"] = item["evidence"][0]["vector"]
            status, body = s.req("PUT", "/v1/lessons", {"items": [bare]}, s.alice)
            assert status == 422 and "'vector'" in body["error"]
        finally:
            s.close()
    s = Srv(tmp_path / "textless", max_level="evidence")
    try:
        bare = item_for([make_ev("n")], "lessons")  # level (a) payload: no quote, no finding
        bare["evidence"][0]["vector"] = {"model": "m", "digest": "d", "dim": 2, "b64": embed.to_b64(embed.pack([1.0, 0.0]))}
        status, body = s.req("PUT", "/v1/lessons", {"items": [bare]}, s.alice)
        assert status == 200 and body["results"][0]["status"] == "rejected" and "text" in body["results"][0]["error"]
        broken = with_vectors(item_for([ev], "evidence"), [ev], emb)
        broken["evidence"][0]["vector"]["b64"] = "###"
        assert s.req("PUT", "/v1/lessons", {"items": [broken]}, s.alice)[0] == 422
    finally:
        s.close()


def test_the_level_a_client_embeds_no_text_and_attaches_nothing(tmp_path, fake):
    db, _s, kind = seeded(tmp_path, 2, signal="verdict_valid")
    ctx = make_ctx(tmp_path, tmp_path, memory=cfg_for(tmp_path))
    semantic.refresh_local(ctx)
    evs = db.evidence()
    item = levels.lesson_item(evs[0].lesson_id, kind, evs, "lessons")
    pin = Pin("fake-embed", DIGEST, DIM)
    assert semantic.attach_vectors(ctx, db, item, evs, "lessons", pin) == 0 and all("vector" not in r for r in item["evidence"])
    item_b = levels.lesson_item(evs[0].lesson_id, kind, evs, "evidence")
    assert semantic.attach_vectors(ctx, db, item_b, evs, "evidence", pin) == 2


# ---------- sync: a digest mismatch sends no vectors ----------

@pytest.fixture
def clone(tmp_path):
    return make_origin_and_clone(tmp_path)[1]


def machine(tmp_path, clone, srv, name, token, digest=DIGEST, monkeypatch=None, **mem):
    root = tmp_path / name
    root.mkdir()
    tf = root / "token"
    tf.write_text(token + "\n")
    tf.chmod(0o600)
    provider = FakeProvider()
    provider.private = False
    cfg = MemoryConfig(store=str(root / "mem"), url=f"http://127.0.0.1:{srv.port}", token_file=str(tf), share_level="evidence",
                       share_repos=("o/r",), embed_model="fake-embed", min_similarity=0.3, **mem)
    return make_ctx(root, clone, provider=provider, memory=cfg), MemoryDB(root / "mem")


def test_a_digest_mismatch_sends_no_vectors_and_retrieval_falls_back(tmp_path, clone, srv, monkeypatch):
    monkeypatch.setattr(sync, "clock", lambda: NOW)
    emb = FakeEmbedder(digest="sha256:mine")
    monkeypatch.setattr(semantic, "make_embedder", lambda cfg: emb)
    # the server is pinned by someone else's first vector
    other = FakeEmbedder()
    ev0 = finding_ev("zero")
    pinned = with_vectors(item_for([ev0], "evidence"), [ev0], other)
    assert srv.req("PUT", "/v1/lessons", {"items": [pinned]}, srv.bob)[0] == 200

    ctx, db = machine(tmp_path, clone, srv, "alice", srv.alice)
    mine = finding_ev("mine", msg="a different finding about null input", pr="o/r#2")
    db.upsert_evidence([mine])
    semantic.refresh_local(ctx)
    pushed = sync.sync(ctx, do_pull=False)
    assert pushed["push"]["pushed"] is True and "mismatch" in pushed["push"]["vectors"]
    assert semantic.vector_stats(srv.db)["vectors"] == 1  # still only the first contributor's
    assert srv.db.counts()["evidence"] == 2  # the lesson itself was shared, without a vector

    # retrieval: the last sync saw the server's pin, which this machine's model does not match -> exact-key only
    assert semantic.ranked_for(ctx, db, "o/r", [semantic.Query("null input")], "finding") == []
    st = call(ctx, "memory", "status")[1]
    assert st["embedding"]["server"]["match"] is False and "exact-key" in st["embedding"]["note"]

    # with the same model the vectors are sent and accepted
    emb._digest = DIGEST
    semantic.refresh_local(ctx)
    pushed2 = sync.sync(ctx, do_pull=False)
    assert pushed2["push"]["vectors"] == "ok" and semantic.vector_stats(srv.db)["vectors"] == 2


# ---------- the server's search ----------

def test_search_is_404_without_allow_search(srv):
    srv.service.embedder = FakeEmbedder()
    status, _ = srv.req("POST", "/v1/search", {"repo": "o/r", "queries": [{"text": "x"}]}, srv.alice)
    assert status == 404
    assert srv.req("POST", "/v1/search", {"repo": "o/r", "queries": [{"text": "x"}]})[0] == 401  # authenticated like the rest


def test_search_ranks_stores_nothing_and_logs_no_query_text(srv):
    srv.service.embedder, srv.service.allow_search = FakeEmbedder(), True
    evs = [finding_ev("a", msg="user input is not checked for null"), finding_ev("b", msg="database migration lacks rollback",
                                                                                 scope=Scope("github.com", "o/r", "db/**", "sql", "tests"))]
    for e in evs:
        assert srv.req("PUT", "/v1/lessons", {"items": [item_for([e], "evidence")]}, srv.alice)[0] == 200
    marker = "zzqueryxx marker for null input"
    status, body = srv.req("POST", "/v1/search", {"repo": "o/r", "limit": 5, "kind": "finding",
                                                  "queries": [{"text": marker, "path": "a.py", "category": "x", "language": "python"}]}, srv.alice)
    assert status == 200 and body["results"][0]["lesson_id"] == evs[0].lesson_id and body["results"][0]["score"] > 0
    assert body["model"] == "fake-embed"
    assert all(marker not in line and "zzqueryxx" not in line for line in srv.lines)
    srv.service.db.recompute()  # settle WAL content
    blob = b"".join(p.read_bytes() for p in Path(srv.db.file).parent.iterdir() if p.is_file())
    assert b"zzqueryxx" not in blob
    assert semantic.vector_stats(srv.db)["vectors"] == 2  # the server embedded the evidence it holds, nothing else
    assert srv.req("POST", "/v1/search", {"repo": "o/r", "queries": []}, srv.alice)[0] == 422
    assert srv.req("POST", "/v1/search", {"repo": "o/r", "queries": [{"text": "y" * 401}]}, srv.alice)[0] == 422
    assert srv.req("POST", "/v1/search", {"repo": "bad repo", "queries": [{"text": "y"}]}, srv.alice)[0] == 422


def test_search_is_503_when_the_embedding_service_is_down(srv):
    srv.service.embedder, srv.service.allow_search = FakeEmbedder(fail=True), True
    assert srv.req("POST", "/v1/search", {"repo": "o/r", "queries": [{"text": "x"}]}, srv.alice)[0] == 503


# ---------- server mode on the client ----------

def with_mem(ctx, **over):
    other = copy.copy(ctx)
    other.config = replace(ctx.config, memory=replace(ctx.config.memory, **over))
    return other


def test_server_mode_is_refused_below_level_evidence_for_private_unknown_and_unlisted_repos(tmp_path, clone, srv):
    srv.service.embedder, srv.service.allow_search = FakeEmbedder(), True
    srv.req("PUT", "/v1/lessons", {"items": [item_for([finding_ev()], "evidence")]}, srv.alice)  # pins the server
    ctx, _db = machine(tmp_path, clone, srv, "m", srv.alice, embed_at="server")
    t = sync.build_transport(ctx)
    assert semantic.server_mode_refusal(ctx, "o/r", t) is None
    ctx.provider.private = True
    assert "private" in semantic.server_mode_refusal(ctx, "o/r", t)
    ctx.provider.private = None
    assert "unknown" in semantic.server_mode_refusal(ctx, "o/r", t)
    ctx.provider.private = False
    low = with_mem(ctx, share_level="lessons")
    assert "lessons" in semantic.server_mode_refusal(low, "o/r", t)
    assert "share_repos" in semantic.server_mode_refusal(ctx, "x/y", t)
    nosearch = with_mem(ctx, url="")
    assert "memory.url" in semantic.server_mode_refusal(nosearch, "o/r", t)


def test_no_query_is_sent_when_server_mode_is_refused(tmp_path, clone, srv, monkeypatch):
    srv.service.embedder, srv.service.allow_search = FakeEmbedder(), True
    ctx, db = machine(tmp_path, clone, srv, "m", srv.alice, embed_at="server")
    ctx.provider.private = True
    sent = []
    monkeypatch.setattr(HttpTransport, "search", lambda self, body: sent.append(body), raising=False)
    assert semantic.ranked_for(ctx, db, "o/r", [semantic.Query("secret code")], "finding") == []
    assert sent == []
    assert not any("secret" in line for line in srv.lines)


def test_a_server_without_search_makes_the_client_fall_back(tmp_path, clone, srv):
    ctx, db = machine(tmp_path, clone, srv, "m", srv.alice, embed_at="server")
    assert semantic.ranked_for(ctx, db, "o/r", [semantic.Query("null input")], "finding") == []


def test_client_and_server_mode_rank_the_same(tmp_path, clone, srv, monkeypatch):
    emb = FakeEmbedder()
    monkeypatch.setattr(semantic, "make_embedder", lambda cfg: emb)
    srv.service.embedder, srv.service.allow_search = emb, True
    texts = ["user input is not checked for null", "database migration lacks a rollback", "null input reaches strip"]
    scopes = [Scope("github.com", "o/r", f"d{i}/**", "python", "tests") for i in range(3)]
    evs = [finding_ev(f"t{i}", msg=t, scope=sc, pr=f"o/r#{i}") for i, (t, sc) in enumerate(zip(texts, scopes, strict=True))]
    ctx, db = machine(tmp_path, clone, srv, "m", srv.alice, embed_at="client")
    db.upsert_evidence(evs)
    semantic.refresh_local(ctx)
    for e in evs:
        assert srv.req("PUT", "/v1/lessons", {"items": [item_for([e], "evidence")]}, srv.alice)[0] == 200
    qs = [semantic.Query("a null input is passed to strip")]
    client_side = semantic.client_ranked(ctx, db, "o/r", qs, "finding", 3.0)
    server_ctx = with_mem(ctx, embed_at="server")
    server_side = semantic.server_ranked(server_ctx, "o/r", qs, "finding", 3.0)
    assert client_side and [lid for lid, _ in client_side] == [lid for lid, _ in server_side]
    assert [round(s, 4) for _, s in client_side] == [round(s, 4) for _, s in server_side]


# ---------- config and CLI ----------

def test_min_similarity_is_a_validated_key(tmp_path):
    def cfg(extra):
        home = tmp_path / "home"
        (home / ".config/code-check").mkdir(parents=True, exist_ok=True)
        (home / ".config/code-check/config.toml").write_text(extra)
        return load_config(tmp_path / "repo", home, {})

    assert cfg("[memory]\nmin_similarity = 0.7\n").memory.min_similarity == 0.7
    assert cfg("[memory]\nmin_similarity = 1\n").memory.min_similarity == 1.0
    for bad in ("1.5", "-0.1", '"x"'):
        with pytest.raises(ConfigError):
            cfg(f"[memory]\nmin_similarity = {bad}\n")
    assert load_config(tmp_path / "repo", tmp_path / "home2", {"CODE_CHECK_MEMORY__MIN_SIMILARITY": "0.8"}).memory.min_similarity == 0.8


def test_memory_embed_and_status_report_the_embedding_state(tmp_path, clone, fake):
    seeded(tmp_path, 2)
    ctx = make_ctx(tmp_path, clone, memory=cfg_for(tmp_path))
    code, out = call(ctx, "memory", "embed")
    assert code == 0 and out["embedded"] == 2 and out["model"] == "fake-embed"
    code, st = call(ctx, "memory", "status", "--offline")
    assert code == 0 and st["embedding"]["enabled"] and st["embedding"]["local"]["vectors"] == 2
    code, st = call(ctx, "memory", "status")
    assert st["embedding"]["ollama"]["reachable"] is True and st["embedding"]["digest_matches_vectors"] is True
    code, again = call(ctx, "memory", "embed", "--rebuild")
    assert again["cleared"] == 2 and again["embedded"] == 2


def test_the_embedding_section_is_minimal_when_embeddings_are_off(tmp_path, clone):
    ctx = make_ctx(tmp_path, clone, memory=MemoryConfig(store=str(tmp_path / "mem")))
    code, st = call(ctx, "memory", "status", "--offline")
    assert code == 0 and st["embedding"] == {"enabled": False, "model": None, "url": "http://127.0.0.1:11434", "at": "client",
                                             "min_similarity": 0.60}
    assert call(ctx, "memory", "embed")[1]["embedded"] == 0


def test_the_solo_service_answers_search_like_the_server(tmp_path):
    svc = Service(ServerDB(tmp_path / "solo", now=lambda: NOW).init(), now=lambda: NOW)
    t = LocalTransport(svc)
    assert t.search({"repo": "o/r", "queries": [{"text": "x"}]}).status == 404
    svc.allow_search, svc.embedder = True, FakeEmbedder()
    assert t.search({"repo": "o/r", "queries": [{"text": "x"}]}).status == 200
