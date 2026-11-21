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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from codecheck.errors import CodeCheckError
from codecheck.memory import levels
from codecheck.memory.consolidate import DAY, Params
from codecheck.memory.db import (
    SCHEMA_VERSION,
    MemoryDB,
    MemoryStoreError,
    _evidence_from,
)
from codecheck.memory.model import Evidence
from codecheck.shadow.scrub import scrub_text

API_VERSION = 1
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
ROLES = ("contributor", "admin")
PAGE_LIMIT = 500
LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}

_EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (id INTEGER PRIMARY KEY, name TEXT NOT NULL, hash TEXT NOT NULL, role TEXT NOT NULL,
    created_ts REAL NOT NULL, revoked_ts REAL);
CREATE INDEX IF NOT EXISTS tokens_name ON tokens (name);
CREATE TABLE IF NOT EXISTS evidence_contrib (evidence_id TEXT NOT NULL, contributor TEXT NOT NULL, received_ts REAL NOT NULL,
    PRIMARY KEY (evidence_id, contributor));
CREATE INDEX IF NOT EXISTS contrib_who ON evidence_contrib (contributor);
CREATE TABLE IF NOT EXISTS vectors (lesson_id TEXT NOT NULL, model TEXT NOT NULL, digest TEXT NOT NULL, dim INTEGER NOT NULL,
    vec BLOB NOT NULL, PRIMARY KEY (lesson_id, model));
CREATE TABLE IF NOT EXISTS episodes (id INTEGER PRIMARY KEY, repo TEXT NOT NULL, pr TEXT NOT NULL, run_id TEXT NOT NULL,
    body_json TEXT NOT NULL, contributor TEXT NOT NULL, received_ts REAL NOT NULL, UNIQUE (contributor, run_id));
"""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ServerDB(MemoryDB):
    """The shared store. Opening it creates the server tables next to the lesson tables."""

    def init(self) -> ServerDB:
        with self._use(write=True) as conn, self._tx(conn):
            for stmt in _EXTRA_SCHEMA.split(";"):
                if stmt.strip():
                    conn.execute(stmt)
        return self

    # ---- tokens ----

    def add_token(self, name: str, role: str = "contributor") -> str:
        if not NAME_RE.fullmatch(name):
            raise CodeCheckError("a token name must be 1 to 64 letters, digits, '.', '_' or '-'")
        if role not in ROLES:
            raise CodeCheckError(f"role must be one of {', '.join(ROLES)}")
        token = "cck_" + secrets.token_urlsafe(32)
        with self._use(write=True) as conn, self._tx(conn):
            if conn.execute("SELECT 1 FROM tokens WHERE name = ? AND revoked_ts IS NULL", (name,)).fetchone():
                raise CodeCheckError(f"a token named {name!r} already exists; revoke it first")
            conn.execute("INSERT INTO tokens (name, hash, role, created_ts) VALUES (?, ?, ?, ?)",
                         (name, hash_token(token), role, self.now()))
        return token

    def revoke_token(self, name: str) -> bool:
        with self._use(write=True) as conn, self._tx(conn):
            return conn.execute("UPDATE tokens SET revoked_ts = ? WHERE name = ? AND revoked_ts IS NULL",
                                (self.now(), name)).rowcount > 0

    def list_tokens(self) -> list[dict]:
        with self._use(write=False) as conn:
            if conn is None:
                return []
            rows = conn.execute("SELECT name, role, created_ts, revoked_ts FROM tokens ORDER BY id")
            return [{"name": r["name"], "role": r["role"], "created_ts": r["created_ts"], "revoked": r["revoked_ts"] is not None}
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

    # ---- evidence ----

    def ingest(self, contributor: str, items: Sequence[Evidence], params: Params, now: float) -> Counter:
        """Store evidence from one contributor. Contribution rows go first: an orphan row is harmless, while evidence without
        one could never be revoked."""
        live = [e for e in items if (now - e.ts) / DAY < params.expire_days]
        counts: Counter = Counter(skipped_expired=len(items) - len(live))
        with self._use(write=True) as conn, self._tx(conn):
            conn.executemany(
                "INSERT INTO evidence_contrib (evidence_id, contributor, received_ts) VALUES (?, ?, ?) "
                "ON CONFLICT (evidence_id, contributor) DO UPDATE SET received_ts = excluded.received_ts",
                [(e.id, contributor, now) for e in live])
        counts.update(self.upsert_evidence(live))
        return counts

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
        with self._use(write=True) as conn, self._tx(conn):
            conn.execute("DELETE FROM evidence_contrib WHERE contributor = ?", (contributor,))
            gone = conn.execute("DELETE FROM evidence WHERE id NOT IN (SELECT evidence_id FROM evidence_contrib)").rowcount
            eps = conn.execute("DELETE FROM episodes WHERE contributor = ?", (contributor,)).rowcount
        self.recompute(params, now)
        return {"evidence": gone, "episodes": eps}

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

    def __init__(self, db: ServerDB, max_level: str = "episodes", params: Params | None = None,
                 now: Callable[[], float] = time.time) -> None:
        if max_level not in levels.LEVELS:
            raise CodeCheckError(f"max level must be one of {', '.join(levels.LEVELS)}")
        self.db, self.max_level, self.params, self.now = db, max_level, params or Params(), now

    def meta(self) -> tuple[int, dict]:
        return 200, {"api": API_VERSION, "schema": SCHEMA_VERSION, "max_level": self.max_level, "embedding": None}

    def put_lessons(self, contributor: str, body: object) -> tuple[int, dict]:
        items = body.get("items") if isinstance(body, dict) and set(body) == {"items"} else None
        if not isinstance(items, list) or not items or len(items) > levels.MAX_BATCH_ITEMS:
            return 422, {"error": f"body must be {{\"items\": [1 to {levels.MAX_BATCH_ITEMS} lessons]}}"}
        now = self.now()
        parsed, results = [], []
        for raw in items:
            claimed = raw.get("lesson_id") if isinstance(raw, dict) and isinstance(raw.get("lesson_id"), str) else None
            try:
                lid, evidence = levels.parse_item(raw, self.max_level, now)
            except levels.LevelError as e:
                return 422, {"error": str(e), "max_level": self.max_level}  # a config mismatch: nothing is stored
            except levels.PayloadError as e:
                results.append({"lesson_id": claimed, "status": "rejected", "error": str(e)})
                continue
            parsed.append((lid, evidence))
            results.append({"lesson_id": lid, "status": "ok", "error": None})
        counts = self.db.ingest(contributor, [e for _, ev in parsed for e in ev], self.params, now) if parsed else Counter()
        if parsed:
            self.db.recompute(self.params, now)
        return 200, {"results": results, "evidence": dict(counts)}

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
        return 200, {"lessons": out, "evidence": evidence, "next_cursor": nxt, "server_ts": self.now()}

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

def is_loopback(host: str) -> bool:
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


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


class MemoryHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, addr, service: Service, *, allowed_hosts: set[str] | None, max_concurrent: int,
                 log: Callable[[str], None]) -> None:
        self.service, self.allowed_hosts, self.log = service, allowed_hosts, log
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

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
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
        if raw is None or not raw.isdigit():
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
        auth = self.headers.get("Authorization", "")
        scheme, _, token = auth.partition(" ")
        who = srv.service.db.authenticate(token.strip()) if scheme.lower() == "bearer" and token.strip() else None
        if who is None:
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
                status, body = svc.put_lessons(name, self._read_body())
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
                log: Callable[[str], None] | None = None) -> MemoryHTTPServer:
    """A bound, not yet serving, server. Refuses a non-loopback bind without TLS or `behind_proxy`."""
    if bool(tls_cert) != bool(tls_key):
        raise CodeCheckError("--tls-cert and --tls-key go together")
    check_bind(host, tls=bool(tls_cert), behind_proxy=behind_proxy)
    hosts = {h.lower() for h in allow_hosts}
    if is_loopback(host):
        allowed: set[str] | None = LOOPBACK_NAMES | {host.lower()} | hosts  # also defeats DNS rebinding
    else:
        allowed = ({host.lower()} | hosts) if hosts else None  # a proxy or TLS name: check only when told which names
    log = log or (lambda line: print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}", file=sys.stderr, flush=True))
    try:
        server = MemoryHTTPServer((host or "0.0.0.0", port), service, allowed_hosts=allowed,  # noqa: S104 - guarded by check_bind
                                  max_concurrent=max_concurrent, log=log)
    except OSError as e:
        raise CodeCheckError(f"cannot bind {host}:{port}: {e.strerror or e}") from None
    if tls_cert:
        server.socket = _tls_context(tls_cert, tls_key).wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    return server
