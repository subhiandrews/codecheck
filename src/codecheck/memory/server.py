"""The memory server: storage plus HTTP, standard library only.

`ServerDB` is the lesson store of `memory/db.py` (same file, same schema) plus tables the server alone needs: named
tokens, who contributed which evidence row, episodes and (from phase 5) vectors. `Service` holds every rule and is what
solo mode calls directly; `make_server` is `Service` behind `http.server`. Rules that matter:

* Counts are never taken from a client. Lessons are recomputed from evidence rows by the same consolidation code the
  client uses, and evidence ids are derived here from host, repo, PR, thread and signal, so the same thread sent by two
  contributors is one row (both are remembered in `evidence_contrib`, so either can be revoked).
* Tokens are random, shown once and stored as SHA-256 hashes; the token name is the contributor id.
* A field above the server's maximum sharing level is a 422 for the whole request, never silently dropped.
* Nothing is logged but method, path without query, status and contributor name: never a header, a body or evidence text.
"""

import contextlib
import hashlib
import ipaddress
import json
import re
import secrets
import socket
import ssl
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from codecheck.config import LOOPBACK_NAMES as _LOOPBACK_NAMES
from codecheck.config import is_loopback_host
from codecheck.errors import CodeCheckError
from codecheck.memory import levels
from codecheck.memory.consolidate import DAY, Params
from codecheck.memory.db import (
    SCHEMA_VERSION,
    MemoryDB,
    MemoryStoreError,
    _evidence_from,
    drop_orphan_vectors,
)
from codecheck.memory.model import Evidence
from codecheck.shadow.scrub import scrub_text

API_VERSION = 1
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
ROLES = ("contributor", "admin")
# What a token may assert about the people behind its evidence. `maintainer` is the only evidence role that earns more
# than a contributor's weight (consolidate.AGAINST_BY_ROLE / MISS_BY_ROLE) and the only one that can activate a lesson, so
# it is the only one a token needs permission for: self, author, contributor and unknown never weigh more than
# `contributor` for any signal, so they pass unchanged. A client's claim is a request, never a fact.
CEILINGS = ("contributor", "maintainer")
PURGE_LOG_MAX = 20000
PURGED_PER_RESPONSE = 1000
PAGE_LIMIT = 500
LOOPBACK_NAMES = set(_LOOPBACK_NAMES)

_EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (id INTEGER PRIMARY KEY, name TEXT NOT NULL, hash TEXT NOT NULL, role TEXT NOT NULL,
    created_ts REAL NOT NULL, revoked_ts REAL);
CREATE INDEX IF NOT EXISTS tokens_name ON tokens (name);
CREATE TABLE IF NOT EXISTS evidence_contrib (evidence_id TEXT NOT NULL, contributor TEXT NOT NULL, received_ts REAL NOT NULL,
    PRIMARY KEY (evidence_id, contributor));
CREATE TABLE IF NOT EXISTS purge_log (id INTEGER PRIMARY KEY, evidence_id TEXT NOT NULL, repo TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS purge_log_repo_ts ON purge_log (repo, ts);
CREATE INDEX IF NOT EXISTS contrib_who ON evidence_contrib (contributor);
CREATE TABLE IF NOT EXISTS vectors (lesson_id TEXT NOT NULL, model TEXT NOT NULL, digest TEXT NOT NULL, dim INTEGER NOT NULL,
    vec BLOB NOT NULL, PRIMARY KEY (lesson_id, model));
CREATE TABLE IF NOT EXISTS episodes (id INTEGER PRIMARY KEY, repo TEXT NOT NULL, pr TEXT NOT NULL, run_id TEXT NOT NULL,
    body_json TEXT NOT NULL, contributor TEXT NOT NULL, received_ts REAL NOT NULL, UNIQUE (contributor, run_id));
"""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _add_column(conn, table: str, column: str, decl: str) -> None:
    if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


# Best first: when two contributors disagree about who wrote a thread, the heavier role is kept, but a contributor can
# only ever have asserted a role within its own ceiling (`clamp_role`).
_ROLE_ORDER = ("maintainer", "contributor", "self", "unknown", "author")


def clamp_role(role: str, ceiling: str) -> str:
    """`role` as a token with `ceiling` may assert it: a claimed maintainer without that ceiling is a contributor."""
    return "contributor" if role == "maintainer" and ceiling != "maintainer" else role


class ServerDB(MemoryDB):
    """The shared store. Opening it creates the server tables next to the lesson tables."""

    # Ingest writes contribution rows and evidence in two transactions; a purge between them would leave evidence no
    # purge can reach. One lock makes the two operations exclusive within this process.
    _ingest_purge_lock = threading.RLock()

    def init(self) -> ServerDB:
        with self._use(write=True) as conn, self._tx(conn):
            for stmt in _EXTRA_SCHEMA.split(";"):
                if stmt.strip():
                    conn.execute(stmt)
            _add_column(conn, "tokens", "ceiling", "TEXT NOT NULL DEFAULT 'contributor'")  # older stores: the safe ceiling
            _add_column(conn, "evidence_contrib", "role", "TEXT")  # the role this contributor was allowed to assert
        return self

    # ---- tokens ----

    def add_token(self, name: str, role: str = "contributor", ceiling: str = "contributor") -> str:
        if ceiling not in CEILINGS:
            raise CodeCheckError(f"ceiling must be one of {', '.join(CEILINGS)}")
        if not NAME_RE.fullmatch(name):
            raise CodeCheckError("a token name must be 1 to 64 letters, digits, '.', '_' or '-'")
        if role not in ROLES:
            raise CodeCheckError(f"role must be one of {', '.join(ROLES)}")
        token = "cck_" + secrets.token_urlsafe(32)
        with self._use(write=True) as conn, self._tx(conn):
            if conn.execute("SELECT 1 FROM tokens WHERE name = ? AND revoked_ts IS NULL", (name,)).fetchone():
                raise CodeCheckError(f"a token named {name!r} already exists; revoke it first")
            conn.execute("INSERT INTO tokens (name, hash, role, created_ts, ceiling) VALUES (?, ?, ?, ?, ?)",
                         (name, hash_token(token), role, self.now(), ceiling))
        return token

    def revoke_token(self, name: str) -> bool:
        with self._use(write=True) as conn, self._tx(conn):
            return conn.execute("UPDATE tokens SET revoked_ts = ? WHERE name = ? AND revoked_ts IS NULL",
                                (self.now(), name)).rowcount > 0

    def list_tokens(self) -> list[dict]:
        with self._use(write=False) as conn:
            if conn is None:
                return []
            rows = conn.execute("SELECT name, role, ceiling, created_ts, revoked_ts FROM tokens ORDER BY id")
            return [{"name": r["name"], "role": r["role"], "ceiling": r["ceiling"], "created_ts": r["created_ts"], "revoked": r["revoked_ts"] is not None}
                    for r in rows]

    def authenticate(self, token: str) -> tuple[str, str] | None:
        """(name, role) for a live token. Every live hash is compared, in constant time, so timing says nothing."""
        want = hash_token(token)
        found: tuple[str, str] | None = None
        with self._use(write=False) as conn:
            if conn is None:
                return None
            for row in conn.execute("SELECT name, hash, role FROM tokens WHERE revoked_ts IS NULL"):
                if secrets.compare_digest(row["hash"], want):
                    found = (row["name"], row["role"])
        return found

    def ceiling_of(self, name: str) -> str:
        """The highest evidence role the live token `name` may assert; anything unreadable is the lowest."""
        with self._use(write=False) as conn:
            if conn is None:
                return "contributor"
            row = conn.execute("SELECT ceiling FROM tokens WHERE name = ? AND revoked_ts IS NULL", (name,)).fetchone()
        return row["ceiling"] if row and row["ceiling"] in CEILINGS else "contributor"

    # ---- evidence ----

    def ingest(self, contributor: str, items: Sequence[Evidence], params: Params, now: float) -> Counter:
        """Store evidence from one contributor. Contribution rows go first: an orphan row is harmless, while evidence without
        one could never be revoked."""
        live = [e for e in items if (now - e.ts) / DAY < params.expire_days]
        counts: Counter = Counter(skipped_expired=len(items) - len(live))
        with self._ingest_purge_lock:
            with self._use(write=True) as conn, self._tx(conn):
                conn.executemany(
                    "INSERT INTO evidence_contrib (evidence_id, contributor, received_ts, role) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (evidence_id, contributor) DO UPDATE SET received_ts = excluded.received_ts, role = excluded.role",
                    [(e.id, contributor, now, e.role) for e in live])
            counts.update(self.upsert_evidence(live))
            self.resolve_roles([e.id for e in live])
        return counts

    def resolve_roles(self, ids: Sequence[str], *, touch_ts: float | None = None) -> int:
        """Set each stored evidence role to the best role any of its contributors was allowed to assert. Without this a
        later, lower-ceiling copy of the same thread would overwrite a maintainer's role, and a maintainer's role would
        survive that maintainer's purge. Rows with no recorded role (older stores) do not vote. With `touch_ts`, rows
        whose role changed are re-stamped so teammates pull them again."""
        changed = 0
        with self._use(write=True) as conn, self._tx(conn):
            for eid in dict.fromkeys(ids):
                roles = [r[0] for r in conn.execute("SELECT role FROM evidence_contrib WHERE evidence_id = ? AND role IS NOT NULL", (eid,))]
                if not roles:
                    continue
                best = min(roles, key=lambda r: _ROLE_ORDER.index(r) if r in _ROLE_ORDER else len(_ROLE_ORDER))
                if conn.execute("UPDATE evidence SET role = ? WHERE id = ? AND role != ?", (best, eid, best)).rowcount:
                    changed += 1
                    if touch_ts is not None:
                        conn.execute("UPDATE evidence_contrib SET received_ts = ? WHERE evidence_id = ?", (touch_ts, eid))
        return changed

    def purged_page(self, repo: str, after_seq: int, limit: int) -> list[tuple[int, str]]:
        """(sequence, evidence id) purge-log entries of `repo` after `after_seq`, oldest first, minus ids that exist again."""
        with self._use(write=False) as conn:
            if not self._ready(conn):
                return []
            rows = conn.execute(
                "SELECT p.id, p.evidence_id FROM purge_log p WHERE p.repo = ? AND p.id > ? "
                "AND NOT EXISTS (SELECT 1 FROM evidence e WHERE e.id = p.evidence_id) ORDER BY p.id LIMIT ?",
                (repo, after_seq, limit))
            return [(r[0], r[1]) for r in rows]

    def purge_floor(self) -> int:
        """The oldest purge-log sequence still kept (0 when the log is empty)."""
        with self._use(write=False) as conn:
            if not self._ready(conn):
                return 0
            return int(conn.execute("SELECT COALESCE(MIN(id), 0) FROM purge_log").fetchone()[0])

    def purge_seq(self) -> int:
        with self._use(write=False) as conn:
            if not self._ready(conn):
                return 0
            return int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM purge_log").fetchone()[0])

    def evidence_page(self, repo: str, after_ts: float, after_id: str, limit: int) -> list[tuple[float, Evidence]]:
        with self._use(write=False) as conn:
            if not self._ready(conn):
                return []
            rows = conn.execute(
                "SELECT e.*, MAX(c.received_ts) AS rts FROM evidence e JOIN evidence_contrib c ON c.evidence_id = e.id "
                "WHERE e.repo = ? GROUP BY e.id HAVING rts > ? OR (rts = ? AND e.id > ?) ORDER BY rts, e.id LIMIT ?",
                (repo, after_ts, after_ts, after_id, limit))
            return [(r["rts"], _evidence_from(r)) for r in rows]

    def purge_contributor(self, contributor: str, params: Params, now: float) -> dict:
        """Remove what one contributor sent. Evidence another contributor also sent stays, because it is still theirs."""
        with self._ingest_purge_lock, self._use(write=True) as conn, self._tx(conn):
            theirs = [r[0] for r in conn.execute("SELECT evidence_id FROM evidence_contrib WHERE contributor = ?", (contributor,))]
            conn.execute("DELETE FROM evidence_contrib WHERE contributor = ?", (contributor,))
            doomed: list[tuple[str, str]] = []
            for i in range(0, len(theirs), 500):  # only what this contributor sent, and only when nobody else sent it too
                chunk = theirs[i:i + 500]
                doomed += [(r[0], r[1]) for r in conn.execute(
                    f"SELECT id, repo FROM evidence WHERE id IN ({','.join('?' * len(chunk))}) "  # noqa: S608 - placeholders only
                    "AND id NOT IN (SELECT evidence_id FROM evidence_contrib)", chunk)]
            conn.executemany("INSERT INTO purge_log (evidence_id, repo, ts) VALUES (?, ?, ?)", [(i, repo, now) for i, repo in doomed])
            conn.execute("DELETE FROM purge_log WHERE id <= (SELECT MAX(id) FROM purge_log) - ?", (PURGE_LOG_MAX,))
            gone = 0
            for i in range(0, len(doomed), 500):
                chunk = [d[0] for d in doomed[i:i + 500]]
                gone += conn.execute(f"DELETE FROM evidence WHERE id IN ({','.join('?' * len(chunk))})", chunk).rowcount  # noqa: S608 - placeholders only
            eps = conn.execute("DELETE FROM episodes WHERE contributor = ?", (contributor,)).rowcount
            drop_orphan_vectors(conn)  # vectors are derived from the purged evidence text
        reweighed = self.resolve_roles(theirs, touch_ts=now)  # a maintainer's role leaves with the maintainer
        self.recompute(params, now)
        return {"evidence": gone, "episodes": eps, "reweighed": reweighed}

    # ---- episodes ----

    def put_episode(self, contributor: str, body: dict, now: float) -> None:
        with self._use(write=True) as conn, self._tx(conn):
            conn.execute(
                "INSERT INTO episodes (repo, pr, run_id, body_json, contributor, received_ts) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (contributor, run_id) DO UPDATE SET body_json = excluded.body_json, "
                "received_ts = excluded.received_ts",
                (body["repo"], body["pr"], body["run_id"], json.dumps(body, sort_keys=True), contributor, now))

    def counts(self) -> dict:
        with self._use(write=False) as conn:
            if not self._ready(conn):
                return {"evidence": 0, "lessons": 0, "episodes": 0, "tokens": 0}
            n = lambda table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608 - fixed names
            return {"evidence": n("evidence"), "lessons": n("lessons"), "episodes": n("episodes"),
                    "tokens": conn.execute("SELECT count(*) FROM tokens WHERE revoked_ts IS NULL").fetchone()[0]}


class Service:
    """Every server rule, with no HTTP in it. Methods return (status, JSON-able body)."""

    embedder = None  # an Embedder, set by `memory serve --embed-model`
    allow_search = False  # set by `memory serve --allow-search`

    def __init__(self, db: ServerDB, max_level: str = "episodes", params: Params | None = None,
                 now: Callable[[], float] = time.time) -> None:
        if max_level not in levels.LEVELS:
            raise CodeCheckError(f"max level must be one of {', '.join(levels.LEVELS)}")
        self.db, self.max_level, self.params, self.now = db, max_level, params or Params(), now

    def meta(self) -> tuple[int, dict]:
        return 200, {"api": API_VERSION, "schema": SCHEMA_VERSION, "max_level": self.max_level,
                     "embedding": self._embedding()}

    def _embedding(self) -> dict | None:
        # lazy imports: semantic imports the client, which imports this module
        from codecheck.memory import semantic

        return semantic.meta_embedding(self)

    def search(self, body: object) -> tuple[int, dict]:
        """`POST /v1/search` (only with `--allow-search`): ranked lesson ids for query texts. Stores and logs nothing."""
        from codecheck.memory import semantic

        return semantic.serve_search(self, body)

    def put_lessons(self, contributor: str, body: object, ceiling: str = "contributor") -> tuple[int, dict]:
        """`ceiling` is the token's evidence-role ceiling. Solo mode (one person, the same machine) passes "maintainer"."""
        items = body.get("items") if isinstance(body, dict) and set(body) == {"items"} else None
        if not isinstance(items, list) or not items or len(items) > levels.MAX_BATCH_ITEMS:
            return 422, {"error": f"body must be {{\"items\": [1 to {levels.MAX_BATCH_ITEMS} lessons]}}"}
        now = self.now()
        parsed, results, vector_pairs = [], [], []
        for raw in items:
            claimed = raw.get("lesson_id") if isinstance(raw, dict) and isinstance(raw.get("lesson_id"), str) else None
            try:
                lid, evidence = levels.parse_item(raw, self.max_level, now)
            except levels.LevelError as e:
                return 422, {"error": str(e), "max_level": self.max_level}  # a config mismatch: nothing is stored
            except levels.PayloadError as e:
                results.append({"lesson_id": claimed, "status": "rejected", "error": str(e)})
                continue
            capped = [replace(e, role=clamp_role(e.role, ceiling)) for e in evidence]  # never trust the claimed role
            down = sum(1 for a, b in zip(evidence, capped, strict=True) if a.role != b.role)
            parsed.append((lid, capped))
            vector_pairs.extend(zip(raw["evidence"], capped, strict=True))
            results.append({"lesson_id": lid, "status": "ok", "error": None, "downgraded_roles": down})
        vectors: list = []
        if vector_pairs:
            # lazy import: semantic imports the client, which imports this module
            from codecheck.memory import semantic

            refused, vectors = semantic.collect_vectors(self.db, vector_pairs)
            if refused:
                return refused  # 409 for another model, 422 for a malformed vector: nothing is stored
        counts = self.db.ingest(contributor, [e for _, ev in parsed for e in ev], self.params, now) if parsed else Counter()
        if parsed:
            self.db.recompute(self.params, now)
        if vectors:
            semantic.put_vectors(self.db, vectors)
        return 200, {"results": results, "evidence": dict(counts),
                     "downgraded_roles": sum(r.get("downgraded_roles", 0) for r in results)}

    def get_lessons(self, query: dict[str, list[str]]) -> tuple[int, dict]:
        repo = (query.get("repo") or [""])[0]
        if not levels._REPO.fullmatch(repo):
            return 422, {"error": "repo must look like owner/name"}
        try:
            since = float((query.get("since") or ["0"])[0])
            cursor = (query.get("cursor") or [""])[0]
            # `since` is exclusive: "z" sorts after every hex evidence id, so nothing received at exactly `since` repeats
            after_ts, _, after_id = cursor.partition("|") if cursor else (str(since), "", "z")
            after = float(after_ts)
        except ValueError:
            return 422, {"error": "since and cursor are malformed"}
        if cursor and not re.fullmatch(r"[0-9a-f]{1,32}", after_id):
            return 422, {"error": "cursor is malformed"}
        page = self.db.evidence_page(repo, after, after_id, PAGE_LIMIT + 1)
        more = len(page) > PAGE_LIMIT
        page = page[:PAGE_LIMIT]
        ids = {e.lesson_id for _, e in page}
        lessons = [le for le in self.db.lessons(repo=repo) if le.id in ids]
        out = [{
            "lesson_id": le.id, "kind": le.kind, "lean": le.lean, "status": le.status, "statement": le.statement,
            "support": le.support, "contra": le.contra, "n_prs": le.n_prs, "confidence": le.confidence,
            "scope": levels.scope_payload(le.scope)} for le in lessons]
        evidence = [{**levels.evidence_payload(e, "episodes"), "lesson_id": e.lesson_id, "received_ts": ts} for ts, e in page]
        nxt = f"{page[-1][0]!r}|{page[-1][1].id}" if more and page else None
        body = {"lessons": out, "evidence": evidence, "next_cursor": nxt, "server_ts": self.now()}
        if not cursor:  # purged ids ride on the first page of a pull; the sequence number is the client's watermark
            try:
                after_seq = max(int((query.get("purged_since") or ["0"])[0]), 0)
            except ValueError:
                return 422, {"error": "purged_since is malformed"}
            seq_now = self.db.purge_seq()  # read first: a purge landing in between is simply seen by the next pull
            floor = self.db.purge_floor()
            gap = after_seq > 0 and floor > after_seq + 1  # entries the client never saw were pruned from the log
            gone = self.db.purged_page(repo, after_seq, PURGED_PER_RESPONSE + 1)
            truncated = len(gone) > PURGED_PER_RESPONSE
            gone = gone[:PURGED_PER_RESPONSE]
            body.update(purged=[i for _, i in gone], purged_more=truncated, purged_gap=gap,
                        purged_seq=gone[-1][0] if truncated else max(seq_now, after_seq))
        return 200, body

    def put_episodes(self, contributor: str, body: object) -> tuple[int, dict]:
        if levels.rank(self.max_level) < levels.rank("episodes"):
            return 422, {"error": f"episodes are above this server's maximum sharing level ({self.max_level})",
                         "max_level": self.max_level}
        eps = body.get("episodes") if isinstance(body, dict) and set(body) == {"episodes"} else None
        if not isinstance(eps, list) or not eps or len(eps) > levels.MAX_EPISODES_PER_BATCH:
            return 422, {"error": f"body must be {{\"episodes\": [1 to {levels.MAX_EPISODES_PER_BATCH}]}}"}
        now, results = self.now(), []
        for raw in eps:
            try:
                ep = levels.parse_episode(raw)
            except levels.PayloadError as e:
                results.append({"run_id": raw.get("run_id") if isinstance(raw, dict) and isinstance(raw.get("run_id"), str) else None,
                                "status": "rejected", "error": str(e)})
                continue
            self.db.put_episode(contributor, ep, now)
            results.append({"run_id": ep["run_id"], "status": "ok", "error": None})
        return 200, {"results": results}

    def delete_evidence(self, contributor: str) -> tuple[int, dict]:
        if not NAME_RE.fullmatch(contributor or ""):
            return 422, {"error": "contributor is malformed"}
        return 200, {"contributor": contributor, **self.db.purge_contributor(contributor, self.params, self.now())}


# ---------- HTTP ----------

is_loopback = is_loopback_host  # one definition shared with config validation


def check_bind(host: str, *, tls: bool, behind_proxy: bool) -> None:
    """A non-loopback address is refused unless the server speaks TLS itself or is declared to sit behind a TLS proxy."""
    if host and is_loopback(host):
        return
    if not (tls or behind_proxy):
        raise CodeCheckError(
            f"refusing to bind {host or 'all interfaces'}: a non-loopback address needs --tls-cert and --tls-key, "
            "or --behind-proxy when a reverse proxy terminates TLS")


def _host_only(header: str) -> str:
    header = header.strip().lower()
    if header.startswith("["):
        return header[1:].split("]", 1)[0]
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header


_KNOWN_PATHS = frozenset({"/healthz", "/v1/meta", "/v1/lessons", "/v1/episodes", "/v1/evidence", "/v1/search"})


class AuthLimiter:
    """Failed-token throttle, per source address, in memory. After `max_failures` failed authentications within `window`
    seconds an address is locked out for `lockout` seconds: every request from it, valid token or not, is answered 429
    before any token is looked at, so the answer says nothing about which tokens exist. Other addresses are unaffected.
    The table is bounded; the clock is injectable for tests."""

    MAX_ADDRESSES = 4096

    def __init__(self, max_failures: int = 10, window: float = 60.0, lockout: float = 60.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.max_failures, self.window, self.lockout, self.clock = max_failures, window, lockout, clock
        self._fails: dict[str, list[float]] = {}
        self._locked: dict[str, float] = {}
        self._lock = threading.Lock()

    def retry_after(self, addr: str) -> int | None:
        """Seconds until `addr` may try again, or None when it is not locked out."""
        now = self.clock()
        with self._lock:
            until = self._locked.get(addr)
            if until is None:
                return None
            if until <= now:
                del self._locked[addr]
                self._fails.pop(addr, None)
                return None
            return max(int(until - now) + 1, 1)

    def failed(self, addr: str) -> None:
        now = self.clock()
        with self._lock:
            if len(self._fails) >= self.MAX_ADDRESSES and addr not in self._fails:
                self._sweep(now)
                if len(self._fails) >= self.MAX_ADDRESSES:
                    self._fails.pop(next(iter(self._fails)))  # oldest first: a flood of addresses cannot grow the table
            hits = [t for t in self._fails.get(addr, []) if now - t < self.window]
            hits.append(now)
            self._fails[addr] = hits
            if len(hits) >= self.max_failures:
                self._locked[addr] = now + self.lockout

    def _sweep(self, now: float) -> None:
        for a in [a for a, hits in self._fails.items() if not hits or now - hits[-1] >= self.window]:
            del self._fails[a]
        for a in [a for a, until in self._locked.items() if until <= now]:
            del self._locked[a]


def _source(handler_addr: str, forwarded: str, behind_proxy: bool) -> str:
    """The address failures are counted against. Behind a declared proxy that appends to X-Forwarded-For, the last entry is
    the one the proxy itself added; anything else (a client-supplied earlier entry) is never believed."""
    if behind_proxy and forwarded:
        last = forwarded.rsplit(",", 1)[-1].strip()
        try:
            return str(ipaddress.ip_address(last))
        except ValueError:
            pass
    return handler_addr


class MemoryHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, addr, service: Service, *, allowed_hosts: set[str] | None, max_concurrent: int,
                 log: Callable[[str], None], behind_proxy: bool = False, limiter: AuthLimiter | None = None) -> None:
        self.service, self.allowed_hosts, self.log = service, allowed_hosts, log
        self.behind_proxy, self.limiter = behind_proxy, limiter or AuthLimiter()
        self.slots = threading.BoundedSemaphore(max_concurrent)
        self.is_v6 = ":" in addr[0]
        if self.is_v6:
            self.address_family = socket.AF_INET6
        super().__init__(addr, _Handler)


class _Handler(BaseHTTPRequestHandler):
    server: MemoryHTTPServer
    server_version = "code-check-memory"
    sys_version = ""
    timeout = 10  # seconds to read the request line, the headers and the body: a slow sender is dropped

    # Never let the base class write request lines or errors: they could carry a path or a header.
    def log_message(self, *_args) -> None:
        return

    def log_request(self, *_args, **_kw) -> None:
        return

    def log_error(self, *_args) -> None:
        return

    def _send(self, status: int, body: dict, headers: dict[str, str] | None = None) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
        self._status = status

    def _read_body(self) -> object:
        if self.headers.get("Transfer-Encoding"):
            raise levels.PayloadError("chunked bodies are not accepted", 411)
        raw = self.headers.get("Content-Length")
        if raw is None or not (raw.isascii() and raw.isdigit()):
            raise levels.PayloadError("Content-Length is required", 411)
        size = int(raw)
        if size > levels.MAX_BODY_BYTES:
            raise levels.PayloadError(f"body is larger than {levels.MAX_BODY_BYTES} bytes", 413)
        data = self.rfile.read(size)
        if len(data) != size:
            raise levels.PayloadError("body is shorter than Content-Length", 400)
        try:
            return json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise levels.PayloadError("body is not valid JSON", 400) from None

    def _dispatch(self) -> None:
        self._status, who = 500, "-"
        try:
            if not self.server.slots.acquire(blocking=False):
                self._send(503, {"error": "server is busy"})
                return
            try:
                who = self._handle()
            finally:
                self.server.slots.release()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, ssl.SSLError):
            self.close_connection = True
        except Exception as e:  # noqa: BLE001 - one request must never take the server down
            with contextlib.suppress(OSError):
                self._send(500, {"error": "internal error"})
            self.server.log(f"error {type(e).__name__}")
        finally:
            path = urlsplit(self.path).path[:100]
            if path not in _KNOWN_PATHS:
                path = "/other"  # an unknown path is client text: it could be a pasted token, so it is never logged
            self.server.log(f"{self.command} {scrub_text(path)} {self._status} {who}")

    def _handle(self) -> str:
        srv = self.server
        allowed = srv.allowed_hosts
        if allowed is not None and _host_only(self.headers.get("Host", "")) not in allowed:
            self._send(421, {"error": "unexpected Host header"})
            return "-"
        url = urlsplit(self.path)
        if url.path == "/healthz" and self.command == "GET":
            self._send(200, {"ok": True})
            return "-"
        source = _source(str(self.client_address[0]), self.headers.get("X-Forwarded-For", ""), srv.behind_proxy)
        wait = srv.limiter.retry_after(source)
        if wait is not None:  # before any token is looked at, so a lockout never reveals whether a token is valid
            self._send(429, {"error": "too many failed attempts; try again later"}, {"Retry-After": str(wait)})
            return "-"
        auth = self.headers.get("Authorization", "")
        scheme, _, token = auth.partition(" ")
        who = srv.service.db.authenticate(token.strip()) if scheme.lower() == "bearer" and token.strip() else None
        if who is None:
            srv.limiter.failed(source)
            self._send(401, {"error": "a valid token is required"})
            return "-"
        name, role = who
        svc = srv.service
        route = (self.command, url.path)
        try:
            if route == ("GET", "/v1/meta"):
                status, body = svc.meta()
            elif route == ("GET", "/v1/lessons"):
                status, body = svc.get_lessons(parse_qs(url.query))
            elif route == ("PUT", "/v1/lessons"):
                status, body = svc.put_lessons(name, self._read_body(), svc.db.ceiling_of(name))
            elif route == ("POST", "/v1/search"):
                status, body = svc.search(self._read_body())
            elif route == ("PUT", "/v1/episodes"):
                status, body = svc.put_episodes(name, self._read_body())
            elif route == ("DELETE", "/v1/evidence"):
                if role != "admin":
                    status, body = 403, {"error": "an admin token is required"}
                else:
                    status, body = svc.delete_evidence((parse_qs(url.query).get("contributor") or [""])[0])
            else:
                status, body = 404, {"error": "not found"}
        except levels.PayloadError as e:
            status, body = e.code, {"error": str(e)}
        except MemoryStoreError:
            status, body = 503, {"error": "the store is unavailable"}
        self._send(status, body)
        return name

    do_GET = do_PUT = do_DELETE = do_POST = do_PATCH = do_HEAD = _dispatch


def _tls_context(cert: str, key: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.load_cert_chain(cert, key)
    except (OSError, ssl.SSLError) as e:
        raise CodeCheckError(f"cannot load the TLS certificate or key: {type(e).__name__}") from None
    return ctx


def make_server(service: Service, host: str = "127.0.0.1", port: int = 8787, *, tls_cert: str = "", tls_key: str = "",
                behind_proxy: bool = False, allow_hosts: Sequence[str] = (), max_concurrent: int = 32,
                log: Callable[[str], None] | None = None, limiter: AuthLimiter | None = None) -> MemoryHTTPServer:
    """A bound, not yet serving, server. Refuses a non-loopback bind without TLS or `behind_proxy`, and without `allow_hosts`."""
    if bool(tls_cert) != bool(tls_key):
        raise CodeCheckError("--tls-cert and --tls-key go together")
    check_bind(host, tls=bool(tls_cert), behind_proxy=behind_proxy)
    hosts = {h.lower() for h in allow_hosts}
    if is_loopback(host):
        allowed: set[str] | None = LOOPBACK_NAMES | {host.lower()} | hosts  # also defeats DNS rebinding
    else:
        if not hosts:
            raise CodeCheckError("a server reachable from other machines needs at least one --allow-host (the public hostname "
                                 "clients use, for example memory.example.com): without it any Host header is accepted")
        allowed = {host.lower()} | hosts
    log = log or (lambda line: print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}", file=sys.stderr, flush=True))
    try:
        server = MemoryHTTPServer((host or "0.0.0.0", port), service, allowed_hosts=allowed,  # noqa: S104 - guarded by check_bind
                                  max_concurrent=max_concurrent, log=log, behind_proxy=behind_proxy, limiter=limiter)
    except OSError as e:
        raise CodeCheckError(f"cannot bind {host}:{port}: {e.strerror or e}") from None
    if tls_cert:
        server.socket = _tls_context(tls_cert, tls_key).wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    return server
