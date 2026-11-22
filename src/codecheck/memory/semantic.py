"""Semantic retrieval: rank the evidence behind lessons by similarity to the code or findings under review.

Everything here is optional and fails open. With `memory.embed_model` empty (the default) nothing in this module runs
and retrieval is the exact-key lookup of `retrieve.py`. With a model set, `ranked_for` returns `(lesson_id, score)` pairs
that `retrieve.merge_ranked` merges after the exact-key hits; any failure (no Ollama, a timeout, a pin mismatch, a server
that does not search) returns `[]`, so a review is never worse off than exact-key.

What is embedded is each evidence item's own text, never the templated lesson statement: a finding's message plus its
outcome label, or a human comment for a miss. Evidence without text (a teammate's evidence shared at level `lessons`) has
no vector and is reachable by exact key only. Vectors live in `evidence_vectors` (evidence_id, model, digest, dim, vec) of
the local replica and of the server, created on demand and never part of `PRAGMA user_version`. A model is a `Pin`
(model, digest, dim); vectors of different pins are never compared.

Two places can embed (`memory.embed_at`): the client (default; the server never sees a query) or the server
(`memory serve --allow-search`; the client sends scrubbed query text, which counts as sharing level `evidence`).
"""

import contextlib
import json
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from codecheck.config import expand_path
from codecheck.memory import embed, levels
from codecheck.memory.db import MemoryDB
from codecheck.memory.embed import (
    MAX_CANDIDATES,
    MAX_QUERIES,
    QUERY_CAP,
    Embedder,
    OllamaEmbedder,
    Pin,
)
from codecheck.memory.model import Evidence
from codecheck.shadow.scrub import scrub_text

TABLE = "evidence_vectors"
PIN_KEY = "embedding"
FILL_LIMIT = 64  # evidence a server embeds per search
MAX_SEARCH_LIMIT = 100
_DDL = (f"CREATE TABLE IF NOT EXISTS {TABLE} (evidence_id TEXT PRIMARY KEY, model TEXT NOT NULL, digest TEXT NOT NULL, "
        "dim INTEGER NOT NULL, vec BLOB NOT NULL)")


@dataclass(frozen=True)
class Query:
    text: str
    path: str = ""
    category: str = ""
    language: str = ""


def make_embedder(cfg) -> Embedder | None:
    """The embedder `memory.*` asks for, or None when embeddings are off. Tests replace this."""
    if not cfg.embed_model:
        return None
    return OllamaEmbedder(cfg.embed_url, cfg.embed_model)


# ---------- text ----------

def evidence_text(ev: Evidence) -> str | None:
    """The text a vector is made from: the finding's message with its outcome, or the human comment of a miss."""
    msg = ev.finding.get("message") if isinstance(ev.finding, dict) else None
    if isinstance(msg, str) and msg.strip():
        return f"{msg.strip()} (outcome: {ev.signal})"
    if isinstance(ev.quote, str) and ev.quote.strip():
        return ev.quote.strip()
    return None


def clean_queries(queries: Iterable[Query]) -> list[Query]:
    """Scrubbed, stripped, capped to 400 characters each, without repeats, at most 40."""
    out, seen = [], set()
    for q in queries:
        text = scrub_text(q.text or "").strip()[:QUERY_CAP]
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(Query(text, q.path, q.category, q.language))
        if len(out) >= MAX_QUERIES:
            break
    return out


def diff_queries(files: dict, reviewable: Iterable[str]) -> list[Query]:
    """One query per changed file: its path and a summary of the added lines. `files` is `parse_diff` output."""
    from codecheck.core.panel import detect_language

    out = []
    for path in reviewable:
        fd = files.get(path)
        if fd is None or getattr(fd, "binary", False):
            continue
        added = " ".join(t for ln in fd.lines if ln.kind == "add" and (t := ln.text.strip()))
        if added:
            out.append(Query(f"{path}: {added}", path, "", detect_language(path) or ""))
    return out


# ---------- the vector table ----------

def _has_table(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)).fetchone() is not None


def put_vectors(db: MemoryDB, rows: Iterable[tuple[str, Pin, bytes]]) -> int:
    rows = list(rows)
    with db._use(write=True) as conn, db._tx(conn):
        conn.execute(_DDL)
        conn.executemany(f"INSERT OR REPLACE INTO {TABLE} (evidence_id, model, digest, dim, vec) VALUES (?, ?, ?, ?, ?)",  # noqa: S608 - fixed name
                         [(eid, p.model, p.digest, p.dim, blob) for eid, p, blob in rows])
    return len(rows)


def vectors_of(db: MemoryDB, evidence_ids: Sequence[str], pin: Pin) -> dict[str, bytes]:
    """The stored vectors of these evidence ids under `pin`."""
    out: dict[str, bytes] = {}
    with db._use(write=False) as conn:
        if not db._ready(conn) or not _has_table(conn):
            return out
        ids = list(evidence_ids)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ", ".join("?" * len(chunk))
            for r in conn.execute(f"SELECT evidence_id, vec FROM {TABLE} WHERE model = ? AND digest = ? AND dim = ? "  # noqa: S608 - fixed name, values bound
                                  f"AND evidence_id IN ({marks})", (pin.model, pin.digest, pin.dim, *chunk)):
                out[r["evidence_id"]] = bytes(r["vec"])
    return out


def candidates(db: MemoryDB, repo: str, model: str, digest: str, kind: str | None = None) -> list[tuple[str, int, bytes]]:
    """(lesson_id, dim, vector) of the newest `MAX_CANDIDATES` evidence of `repo` embedded by this model and digest."""
    where = ""
    if kind == "miss":
        where = " AND e.signal = 'human_miss'"
    elif kind == "finding":
        where = " AND e.signal != 'human_miss'"
    with db._use(write=False) as conn:
        if not db._ready(conn) or not _has_table(conn):
            return []
        rows = conn.execute(
            f"SELECT e.lesson_id AS lid, v.dim AS dim, v.vec AS vec FROM evidence e JOIN {TABLE} v ON v.evidence_id = e.id "  # noqa: S608 - fixed text, values bound
            f"WHERE e.repo = ? AND v.model = ? AND v.digest = ?{where} ORDER BY e.ts DESC, e.id LIMIT ?",
            (repo, model, digest, MAX_CANDIDATES))
        return [(r["lid"], r["dim"], bytes(r["vec"])) for r in rows]


def vector_stats(db: MemoryDB) -> dict:
    """Counts for `memory status`: vectors by (model, digest, dim) and evidence with text that has none yet."""
    with db._use(write=False) as conn:
        if not db._ready(conn):
            return {"vectors": 0, "pins": []}
        if not _has_table(conn):
            return {"vectors": 0, "pins": []}
        pins = [{"model": r["model"], "digest": r["digest"], "dim": r["dim"], "vectors": r["n"]} for r in conn.execute(
            f"SELECT model, digest, dim, count(*) AS n FROM {TABLE} GROUP BY model, digest, dim ORDER BY n DESC")]  # noqa: S608
    return {"vectors": sum(p["vectors"] for p in pins), "pins": pins}


def clear_vectors(db: MemoryDB) -> int:
    with db._use(write=True) as conn, db._tx(conn):
        conn.execute(_DDL)
        return conn.execute(f"DELETE FROM {TABLE}").rowcount  # noqa: S608 - fixed name


def prune_vectors(db: MemoryDB) -> int:
    """Remove vectors whose evidence is gone (forgotten, pruned, revoked)."""
    with db._use(write=True) as conn, db._tx(conn):
        conn.execute(_DDL)
        return conn.execute(f"DELETE FROM {TABLE} WHERE evidence_id NOT IN (SELECT id FROM evidence)").rowcount  # noqa: S608


# ---------- the server's pin ----------

def get_pin(db: MemoryDB) -> Pin | None:
    with db._use(write=False) as conn:
        if not db._ready(conn):
            return None
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (PIN_KEY,)).fetchone()
    try:
        return embed.parse_pin(json.loads(row["value"])) if row else None
    except ValueError:
        return None


def set_pin(db: MemoryDB, pin: Pin) -> Pin:
    """Store `pin` unless one is already stored; returns the pin in force (the first one wins, even in a race)."""
    with db._use(write=True) as conn, db._tx(conn):
        conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (PIN_KEY, json.dumps(pin.as_dict(), sort_keys=True)))
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (PIN_KEY,)).fetchone()
    return embed.parse_pin(json.loads(row["value"])) or pin


def reset_embeddings(db: MemoryDB) -> int:
    with db._use(write=True) as conn, db._tx(conn):
        conn.execute("DELETE FROM meta WHERE key = ?", (PIN_KEY,))
    return clear_vectors(db)


def meta_embedding(svc) -> dict | None:
    """What `GET /v1/meta` reports as `embedding`: the pin and whether this server searches; None until a pin exists."""
    pin = get_pin(svc.db)
    search = bool(getattr(svc, "allow_search", False))
    if pin is not None:
        return {**pin.as_dict(), "search": search}
    emb = getattr(svc, "embedder", None)
    return {"model": emb.model, "digest": None, "dim": None, "search": True} if search and emb is not None else None


# ---------- accepting vectors (server side of PUT /v1/lessons) ----------

def collect_vectors(db: MemoryDB, pairs: Iterable[tuple[object, Evidence]]) -> tuple[tuple[int, dict] | None, list[tuple[str, Pin, bytes]]]:
    """Vectors from the raw evidence items of a batch: `((status, body) | None, rows)`.

    A malformed vector is a 422 and one from another model, digest or dimension than the server's pin is a 409; either
    refuses the whole request and nothing is stored. The first vector a server accepts sets its pin."""
    rows: list[tuple[str, Pin, bytes]] = []
    for raw, ev in pairs:
        v = raw.get("vector") if isinstance(raw, dict) else None
        if v is None:
            continue
        pin = embed.parse_pin(v) if isinstance(v, dict) and set(v) == {"model", "digest", "dim", "b64"} else None
        blob = embed.from_b64(v.get("b64"), pin.dim) if pin else None
        if pin is None or blob is None:
            return (422, {"error": "vector must hold model, digest, dim and the base64 float32 vector of that dimension"}), []
        rows.append((ev.id, pin, blob))
    if not rows:
        return None, []
    wanted = get_pin(db) or rows[0][1]
    if any(p != wanted for _, p, _ in rows):
        return (409, {"error": "the vector's model, digest or dimension does not match this server's embedding model",
                      "embedding": wanted.as_dict()}), []
    if get_pin(db) is None and set_pin(db, wanted) != wanted:  # lost a race with another model's first vector
        return (409, {"error": "the vector's model, digest or dimension does not match this server's embedding model"}), []
    return None, rows


# ---------- embedding local evidence ----------

def refresh_local(ctx, db: MemoryDB | None = None, *, embedder: Embedder | None = None, rebuild: bool = False,
                  budget: float = 20.0) -> dict:
    """Embed the evidence of the local replica that has text and no vector under the current model. Never raises.

    Bounded by `budget` seconds; whatever is not done stays for the next run, and exact-key retrieval is untouched."""
    cfg = ctx.config.memory
    out: dict = {"enabled": bool(cfg.embed_model), "model": cfg.embed_model or None, "embedded": 0, "remaining": 0}
    try:
        if not cfg.embed_model:
            return {**out, "reason": "memory.embed_model is empty: embeddings are off"}
        embedder = embedder or make_embedder(cfg)
        db = db or MemoryDB(expand_path(cfg.store))
        if not db.exists():
            return {**out, "reason": "no store"}
        deadline = time.monotonic() + budget
        digest = embedder.digest() if embedder else None
        if digest is None:
            return {**out, "reason": "the embedding service is unreachable or does not have the model"}
        if rebuild:
            out["cleared"] = clear_vectors(db)
        prune_vectors(db)
        evs = [(e, t) for e in db.evidence() if (t := evidence_text(e))]
        have = _have(db, embedder.model, digest)
        todo = [(e, t) for e, t in evs if e.id not in have]
        out["texts"], out["digest"] = len(evs), digest
        done = 0
        dim = 0
        for i in range(0, len(todo), embed.BATCH):
            left = deadline - time.monotonic()
            if left <= 0:
                break
            chunk = todo[i:i + embed.BATCH]
            vecs = embedder.embed([t for _, t in chunk], left)
            if vecs is None:
                out["reason"] = "the embedding service failed or ran out of time"
                break
            dim = len(vecs[0])
            pin = Pin(embedder.model, digest, dim)
            put_vectors(db, [(e.id, pin, embed.pack(v)) for (e, _), v in zip(chunk, vecs, strict=True)])
            done += len(chunk)
        out.update(embedded=done, remaining=len(todo) - done, skipped_no_text=len(db.evidence()) - len(evs))
        if dim:
            out["dim"] = dim
        return out
    except Exception as e:  # noqa: BLE001 - embedding must never break the caller
        return {**out, "reason": f"error: {type(e).__name__}"}


def _have(db: MemoryDB, model: str, digest: str) -> set[str]:
    with db._use(write=False) as conn:
        if not db._ready(conn) or not _has_table(conn):
            return set()
        return {r[0] for r in conn.execute(f"SELECT evidence_id FROM {TABLE} WHERE model = ? AND digest = ?", (model, digest))}  # noqa: S608


# ---------- ranking ----------

def rank_vectors(cands: Sequence[tuple[str, int, bytes]], qvecs: Sequence[Sequence[float]], min_similarity: float,
                 deadline: float | None = None) -> list[tuple[str, float]]:
    """Lessons ranked by the best similarity of any of their evidence to any query, those at or above `min_similarity`.

    `qvecs` are unit vectors. Ties break on the lesson id, so the order is stable."""
    best: dict[str, float] = {}
    for lid, dim, blob in cands:
        if deadline is not None and time.monotonic() > deadline:
            break
        v = embed.unpack(blob)
        if v is None or len(v) != dim:
            continue
        score = max((embed.dot(v, q) for q in qvecs if len(q) == dim), default=-1.0)
        if score >= min_similarity and score > best.get(lid, -2.0):
            best[lid] = score
    return sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))


def rank_lessons(db: MemoryDB, repo: str, queries: Sequence[Query], embedder: Embedder, *, min_similarity: float,
                 budget: float, kind: str | None = None) -> list[tuple[str, float]] | None:
    """Ranked `(lesson_id, score)` for `queries` against the vectors of `db`, or None when the embedder is unavailable."""
    deadline = time.monotonic() + budget
    qs = clean_queries(queries)
    if not qs:
        return []
    digest = embedder.digest()
    if digest is None:
        return None
    cands = candidates(db, repo, embedder.model, digest, kind)
    if not cands:
        return []
    vecs = embedder.embed([q.text for q in qs], max(deadline - time.monotonic(), 0.0))
    if vecs is None:
        return None
    return rank_vectors(cands, [embed.normalise(v) for v in vecs], min_similarity, deadline)


def _bounded(budget: float, fn):
    """Run `fn` in a daemon thread for at most `budget` seconds; None when it did not finish or raised."""
    box: dict = {}

    def work() -> None:
        try:
            box["out"] = fn()
        except BaseException as e:  # noqa: BLE001 - nothing may escape the worker
            box["error"] = type(e).__name__

    t = threading.Thread(target=work, name="code-check-semantic", daemon=True)
    t.start()
    t.join(max(budget, 0.0))
    return None if t.is_alive() else box.get("out")


def ranked_for(ctx, db: MemoryDB, repo: str, queries: Sequence[Query], kind: str) -> list[tuple[str, float]]:
    """`(lesson_id, score)` for a review, or `[]` whenever semantic retrieval is off or fails. Never raises, and stays
    within half of `memory.timeout_seconds` so the exact-key result is always still on time."""
    try:
        cfg = ctx.config.memory
        if not cfg.embed_model or not queries:
            return []
        budget = min(embed.BUDGET_SECONDS, max(float(cfg.timeout_seconds) * 0.5, 0.1))
        if cfg.embed_at == "server":
            out = _bounded(budget, lambda: server_ranked(ctx, repo, queries, kind, budget))
        else:
            out = _bounded(budget, lambda: client_ranked(ctx, db, repo, queries, kind, budget))
        return out or []
    except Exception:  # noqa: BLE001
        return []


def client_ranked(ctx, db: MemoryDB, repo: str, queries: Sequence[Query], kind: str, budget: float) -> list[tuple[str, float]]:
    cfg = ctx.config.memory
    embedder = make_embedder(cfg)
    if embedder is None:
        return []
    if pin_mismatch(ctx, embedder):
        return []  # the team's model differs from this machine's: exact-key only
    return rank_lessons(db, repo, queries, embedder, min_similarity=cfg.min_similarity, budget=budget, kind=kind) or []


def pin_mismatch(ctx, embedder: Embedder) -> bool:
    """True when the last sync saw a server pin that this machine's model does not match (no network call)."""
    from codecheck.memory import sync

    seen = embed.parse_pin(sync.load_state(ctx).get("server_embedding"))
    if seen is None:
        return False
    digest = embedder.digest()
    return digest is not None and (seen.model, seen.digest) != (embedder.model, digest)


# ---------- server mode: the client side ----------

def server_mode_refusal(ctx, repo: str, transport) -> str | None:
    """Why this repository's query text must not be sent to the server, or None when it may."""
    from codecheck.memory import sync

    m = ctx.config.memory
    if not m.url:
        return "memory.url is not set"
    if repo not in m.share_repos:
        return "the repository is not in memory.share_repos"
    meta = transport.meta()
    server_max = meta.body.get("max_level") if meta.status == 200 else None
    if server_max not in levels.LEVELS:
        return "the server's meta is unavailable"
    private = sync.repo_private(ctx, repo)
    if private is not False:
        return "the repository is private or its visibility is unknown"
    level = levels.effective_level(m.share_level, server_max, m.private_level, private)
    if levels.rank(level) < levels.rank("evidence"):
        return f"the effective sharing level is {level}; query text needs evidence"
    emb = meta.body.get("embedding")
    if not isinstance(emb, dict) or not emb.get("search"):
        return "the server does not offer search"
    return None


def server_ranked(ctx, repo: str, queries: Sequence[Query], kind: str, budget: float) -> list[tuple[str, float]]:
    from codecheck.memory import sync

    m = ctx.config.memory
    qs = clean_queries(queries)
    if not qs:
        return []
    transport = sync.build_transport(ctx, timeout=budget)
    if server_mode_refusal(ctx, repo, transport):
        return []
    body = {"repo": repo, "kind": kind, "limit": min(MAX_SEARCH_LIMIT, max(m.max_lessons * 3, 1)),
            "queries": [{"text": q.text, "path": q.path, "category": q.category, "language": q.language} for q in qs]}
    resp = transport.search(body)
    if resp.status != 200:
        return []  # 404 (the server does not search), 409, 503: exact-key
    out = []
    for r in resp.body.get("results") or []:
        if isinstance(r, dict) and isinstance(r.get("lesson_id"), str) and isinstance(r.get("score"), (int, float)) \
                and r["score"] >= m.min_similarity:
            out.append((r["lesson_id"], float(r["score"])))
    return sorted(out, key=lambda kv: (-kv[1], kv[0]))


# ---------- server mode: the server side ----------

def _server_pin(svc, budget: float) -> Pin | str:
    """The pin this server embeds under, or why it cannot. Sets the pin on first use."""
    emb = svc.embedder
    digest = emb.digest()
    if digest is None:
        return "the embedding service is unavailable"
    stored = get_pin(svc.db)
    if stored is not None:
        if (stored.model, stored.digest) != (emb.model, digest):
            return "the embedding service no longer matches the stored pin; restart with --reset-embeddings"
        return stored
    probe = emb.embed(["probe"], budget)
    if not probe:
        return "the embedding service is unavailable"
    return set_pin(svc.db, Pin(emb.model, digest, len(probe[0])))


def _parse_search(body: object) -> tuple[list[Query], int, str | None] | str:
    """The queries, limit and kind of a search body, or why it is refused."""
    if not isinstance(body, dict) or not {"repo", "queries"} <= set(body) <= {"repo", "queries", "limit", "kind"}:
        return "body must hold repo, queries and optionally limit and kind"
    repo = body["repo"]
    if not isinstance(repo, str) or not levels._REPO.fullmatch(repo):
        return "repo must look like owner/name"
    raw = body["queries"]
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_QUERIES:
        return f"queries must be a list of 1 to {MAX_QUERIES}"
    qs = []
    for q in raw:
        if not isinstance(q, dict) or not {"text"} <= set(q) <= {"text", "path", "category", "language"}:
            return "each query must hold text and optionally path, category and language"
        text = q["text"]
        if not isinstance(text, str) or not text.strip() or len(text) > QUERY_CAP:
            return f"query text must be 1 to {QUERY_CAP} characters"
        extras = [q.get(k, "") for k in ("path", "category", "language")]
        if any(not isinstance(x, str) or len(x) > 200 for x in extras):
            return "path, category and language must be short text"
        qs.append(Query(text, *extras))
    limit = body.get("limit", 12)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_SEARCH_LIMIT:
        return f"limit must be an integer from 1 to {MAX_SEARCH_LIMIT}"
    kind = body.get("kind")
    if kind not in (None, "miss", "finding"):
        return "kind must be miss or finding"
    return qs, limit, kind


def serve_search(svc, body: object) -> tuple[int, dict]:
    """`POST /v1/search`. Stores nothing and logs nothing about the queries: they live in local variables of this call."""
    if not getattr(svc, "allow_search", False):
        return 404, {"error": "not found"}
    parsed = _parse_search(body)
    if isinstance(parsed, str):
        return 422, {"error": parsed}
    qs, limit, kind = parsed
    repo = body["repo"]  # type: ignore[index] - validated by _parse_search
    emb = getattr(svc, "embedder", None)
    if emb is None:
        return 503, {"error": "this server has no embedding model"}
    deadline = time.monotonic() + embed.BUDGET_SECONDS
    pin = _server_pin(svc, embed.BUDGET_SECONDS)
    if isinstance(pin, str):
        return 503, {"error": pin}
    qvecs = emb.embed([scrub_text(q.text) for q in clean_queries(qs)], max(deadline - time.monotonic(), 0.0))
    if qvecs is None:
        return 503, {"error": "the embedding service failed"}
    _fill_missing(svc.db, emb, pin, repo, deadline)
    cands = candidates(svc.db, repo, pin.model, pin.digest, kind)
    ranked = rank_vectors(cands, [embed.normalise(v, pin.dim) for v in qvecs], 0.0, deadline)[:limit]
    return 200, {"results": [{"lesson_id": lid, "score": round(score, 6)} for lid, score in ranked], "model": pin.model}


def _fill_missing(db: MemoryDB, emb: Embedder, pin: Pin, repo: str, deadline: float) -> None:
    """Embed up to `FILL_LIMIT` evidence of `repo` that has text and no vector yet, so a search improves as it is used."""
    with contextlib.suppress(Exception):
        have = _have(db, pin.model, pin.digest)
        todo = [(e, t) for e in db.evidence(repo=repo) if e.id not in have and (t := evidence_text(e))][:FILL_LIMIT]
        for i in range(0, len(todo), embed.BATCH):
            left = deadline - time.monotonic()
            if left <= 0:
                return
            chunk = todo[i:i + embed.BATCH]
            vecs = emb.embed([t for _, t in chunk], left)
            if vecs is None or any(len(v) != pin.dim for v in vecs):
                return
            put_vectors(db, [(e.id, pin, embed.pack(v)) for (e, _), v in zip(chunk, vecs, strict=True)])


# ---------- pushing vectors (client side of sync) ----------

def attach_vectors(ctx, db: MemoryDB, item: dict, evidence: Sequence[Evidence], level: str, pin: Pin) -> int:
    """Add `vector` to the evidence rows of a lesson item that carry text, from the local vectors under `pin`.

    Only at level `evidence` or above (text is shared there) and only while the item stays well under the server's body
    limit. Returns how many were attached."""
    if levels.rank(level) < levels.rank("evidence"):
        return 0
    ordered = sorted(evidence, key=lambda e: (e.ts, e.id))[:levels.MAX_EVIDENCE_PER_ITEM]
    rows = item.get("evidence") or []
    if len(rows) != len(ordered):
        return 0
    have = vectors_of(db, [e.id for e in ordered], pin)
    n, size = 0, len(json.dumps(item))
    for row, e in zip(rows, ordered, strict=True):
        blob = have.get(e.id)
        if blob is None or not (row.get("quote") or (row.get("finding") or {}).get("message")):
            continue
        extra = len(blob) * 4 // 3 + 120
        if size + extra > levels.MAX_BODY_BYTES * 0.7:
            break
        row["vector"] = {**pin.as_dict(), "b64": embed.to_b64(blob)}
        size += extra
        n += 1
    return n


def push_pin(ctx, db: MemoryDB, server_embedding: object, embedder: Embedder | None = None) -> tuple[Pin | None, str]:
    """The pin to send vectors under, or None and why not. Compares this machine's model with the server's."""
    cfg = ctx.config.memory
    if not cfg.embed_model:
        return None, "embeddings are off"
    if cfg.embed_at != "client":
        return None, "embed_at is server: the server embeds"
    embedder = embedder or make_embedder(cfg)
    digest = embedder.digest() if embedder else None
    if digest is None:
        return None, "the embedding service is unreachable"
    stats = [p for p in vector_stats(db)["pins"] if (p["model"], p["digest"]) == (cfg.embed_model, digest)]
    if not stats:
        return None, "no local vectors yet: run `memory embed`"
    pin = Pin(cfg.embed_model, digest, stats[0]["dim"])
    theirs = embed.parse_pin(server_embedding)
    if theirs is not None and (theirs.model, theirs.digest, theirs.dim) != (pin.model, pin.digest, pin.dim):
        return None, "mismatch: the server's embedding model differs from this machine's; no vectors are sent"
    return pin, "ok"


# ---------- status ----------

def status_info(ctx, *, offline: bool = False, server_meta: dict | None = None) -> dict:
    """The embedding section of `memory status`. Makes a network call (to Ollama) unless `offline`."""
    from codecheck.memory import sync

    cfg = ctx.config.memory
    out: dict = {"enabled": bool(cfg.embed_model), "model": cfg.embed_model or None, "url": cfg.embed_url, "at": cfg.embed_at,
                 "min_similarity": cfg.min_similarity}
    if not cfg.embed_model:
        return out
    db = MemoryDB(expand_path(cfg.store))
    stats = vector_stats(db)
    out["local"] = {"vectors": stats["vectors"], "pins": stats["pins"]}
    state = sync.load_state(ctx)
    server_pin = embed.parse_pin((server_meta or {}).get("embedding")) or embed.parse_pin(state.get("server_embedding"))
    out["server"] = {"pin": server_pin.as_dict() if server_pin else None,
                     "search": bool(((server_meta or {}).get("embedding") or {}).get("search"))}
    if offline:
        return out
    digest = make_embedder(cfg).digest()
    out["ollama"] = {"reachable": digest is not None, "digest": digest}
    mine = [p for p in stats["pins"] if p["model"] == cfg.embed_model and p["digest"] == digest]
    out["local"]["current"] = sum(p["vectors"] for p in mine)
    out["digest_matches_vectors"] = bool(mine) if stats["pins"] else None
    if server_pin and digest:
        match = (server_pin.model, server_pin.digest) == (cfg.embed_model, digest)
        out["server"]["match"] = match
        if not match:
            out["note"] = "this machine's model differs from the server's: no vectors are sent and retrieval is exact-key"
    return out
