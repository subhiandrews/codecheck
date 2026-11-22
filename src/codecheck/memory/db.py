"""The local lesson store: one SQLite file (stdlib only) in a 0700 directory, mode 0600.

One connection per use, WAL, a busy timeout and a schema version in `PRAGMA user_version`. Evidence rows are the only
source of truth: lessons are recomputed from them on demand and every count is derived here, so a total sent by anyone
else is never stored. Reading a store that does not exist yields nothing and creates nothing. Any sqlite error is turned
into a `MemoryStoreError`, which the CLI prints as one JSON error, never a traceback.
"""

import contextlib
import json
import os
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from codecheck.errors import CodeCheckError
from codecheck.memory.consolidate import DAY, Params, consolidate
from codecheck.memory.model import Evidence, Lesson, Scope, clean_role

SCHEMA_VERSION = 1
FILE_NAME = "memory.db"
BUSY_MS = 5000

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE evidence (
    id TEXT PRIMARY KEY, lesson_id TEXT NOT NULL, thread_key TEXT NOT NULL,
    host TEXT NOT NULL, repo TEXT NOT NULL, path_glob TEXT NOT NULL, language TEXT NOT NULL, category TEXT NOT NULL,
    signal TEXT NOT NULL, role TEXT NOT NULL, pr TEXT NOT NULL, thread TEXT NOT NULL, ts REAL NOT NULL,
    tool_version TEXT, quote TEXT, finding TEXT
);
CREATE INDEX evidence_lesson ON evidence (lesson_id);
CREATE INDEX evidence_thread ON evidence (thread_key);
CREATE TABLE lessons (
    id TEXT PRIMARY KEY, host TEXT NOT NULL, repo TEXT NOT NULL, path_glob TEXT NOT NULL, language TEXT NOT NULL,
    category TEXT NOT NULL, kind TEXT NOT NULL, lean TEXT NOT NULL, statement TEXT NOT NULL, support REAL NOT NULL,
    contra REAL NOT NULL, n_prs INTEGER NOT NULL, confidence REAL NOT NULL, status TEXT NOT NULL, maintainer INTEGER NOT NULL,
    first_ts REAL NOT NULL, last_ts REAL NOT NULL, expires_ts REAL NOT NULL, provenance TEXT NOT NULL
);
CREATE INDEX lessons_repo ON lessons (repo, status);
CREATE TABLE forgotten (lesson_id TEXT PRIMARY KEY, ts REAL NOT NULL);
"""

_EVIDENCE_COLS = ("id", "lesson_id", "thread_key", "host", "repo", "path_glob", "language", "category", "signal", "role",
                  "pr", "thread", "ts", "tool_version", "quote", "finding")
_LESSON_COLS = ("id", "host", "repo", "path_glob", "language", "category", "kind", "lean", "statement", "support", "contra",
                "n_prs", "confidence", "status", "maintainer", "first_ts", "last_ts", "expires_ts", "provenance")


class MemoryStoreError(CodeCheckError):
    """The memory store cannot be opened or used."""


@dataclass(frozen=True)
class Recomputed:
    lessons: list[Lesson]
    added: int
    changed: int
    removed: int


def _evidence_row(ev: Evidence) -> tuple:
    s = ev.scope
    return (ev.id, ev.lesson_id, ev.thread_key(), s.host, s.repo, s.path_glob, s.language, s.category, ev.signal,
            clean_role(ev.role), ev.pr, ev.thread, float(ev.ts), ev.tool_version, ev.quote,
            json.dumps(ev.finding, sort_keys=True) if ev.finding else None)


def _evidence_from(row: sqlite3.Row) -> Evidence:
    finding = json.loads(row["finding"]) if row["finding"] else None
    return Evidence(
        id=row["id"], lesson_id=row["lesson_id"], scope=Scope(row["host"], row["repo"], row["path_glob"], row["language"], row["category"]),
        signal=row["signal"], ts=row["ts"], pr=row["pr"], thread=row["thread"], role=clean_role(row["role"]),
        tool_version=row["tool_version"], quote=row["quote"], finding=finding)


def _lesson_row(le: Lesson) -> tuple:
    s = le.scope
    return (le.id, s.host, s.repo, s.path_glob, s.language, s.category, le.kind, le.lean, le.statement, le.support,
            le.contra, le.n_prs, le.confidence, le.status, int(le.maintainer), le.first_ts, le.last_ts, le.expires_ts,
            json.dumps(le.provenance, sort_keys=True))


def _lesson_from(row: sqlite3.Row) -> Lesson:
    return Lesson(
        id=row["id"], scope=Scope(row["host"], row["repo"], row["path_glob"], row["language"], row["category"]),
        kind=row["kind"], lean=row["lean"], statement=row["statement"], support=row["support"], contra=row["contra"],
        n_prs=row["n_prs"], confidence=row["confidence"], status=row["status"], first_ts=row["first_ts"],
        last_ts=row["last_ts"], expires_ts=row["expires_ts"], maintainer=bool(row["maintainer"]),
        provenance=json.loads(row["provenance"]))


def drop_orphan_vectors(conn) -> None:
    """Remove the semantic vectors (`evidence_vectors`, made by `memory/semantic.py`) of evidence that no longer exists, in
    the caller's transaction, so evidence removed by a purge, `forget` or expiry leaves nothing derived from its text."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'evidence_vectors'").fetchone():
        conn.execute("DELETE FROM evidence_vectors WHERE evidence_id NOT IN (SELECT id FROM evidence)")


class MemoryDB:
    def __init__(self, directory: Path, now: Callable[[], float] = time.time) -> None:
        self.directory = Path(directory)
        self.file = self.directory / FILE_NAME
        self.now = now

    # ---- connection handling ----

    def exists(self) -> bool:
        return self.file.is_file()

    def _prepare_file(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        fd = os.open(self.file, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)  # never follows a planted symlink
        os.close(fd)
        os.chmod(self.file, 0o600)

    @contextlib.contextmanager
    def _use(self, *, write: bool) -> Iterator[sqlite3.Connection | None]:
        """A connection for one operation, closed afterwards. None when reading a store that does not exist yet."""
        if not self.exists():
            if not write:
                yield None
                return
            try:
                self._prepare_file()
            except OSError as e:
                raise MemoryStoreError(f"cannot create the memory store at {self.directory}: {e.strerror or e}") from e
        conn = None
        try:
            conn = sqlite3.connect(self.file, timeout=BUSY_MS / 1000, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {BUSY_MS}")
            conn.execute("PRAGMA journal_mode = WAL")
            self._migrate(conn, write)
            yield conn
        except sqlite3.DatabaseError as e:
            raise MemoryStoreError(
                f"the memory store {self.file} is unreadable ({e}); fix or delete it and run `memory consolidate` again"
            ) from e
        finally:
            if conn is not None:
                conn.close()

    def _migrate(self, conn: sqlite3.Connection, write: bool) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise MemoryStoreError(f"the memory store {self.file} has schema {version}; this code-check knows {SCHEMA_VERSION}")
        if version == SCHEMA_VERSION:
            return
        tables = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
        if tables:
            raise MemoryStoreError(f"the memory store {self.file} is not a code-check memory store")
        if not write:
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in _SCHEMA.split(";"):
                if stmt.strip():
                    conn.execute(stmt)
            conn.execute("INSERT INTO meta (key, value) VALUES ('created_ts', ?)", (str(self.now()),))
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    @staticmethod
    @contextlib.contextmanager
    def _tx(conn: sqlite3.Connection) -> Iterator[None]:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    @staticmethod
    def _ready(conn: sqlite3.Connection | None) -> bool:
        return conn is not None and conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION

    # ---- evidence ----

    def upsert_evidence(self, items: Iterable[Evidence]) -> Counter:
        """Idempotent on the evidence id. Counts `new`, `updated`, `unchanged`, `stale` (an older signal for a thread that
        already has a newer one) and `forgotten` (its lesson was forgotten). A newer signal for a thread replaces the
        older one, so a thread is never counted twice. Writing the same evidence again changes nothing."""
        out: Counter = Counter()
        with self._use(write=True) as conn, self._tx(conn):
            forgotten = {r[0] for r in conn.execute("SELECT lesson_id FROM forgotten")}
            for ev in items:
                row = _evidence_row(ev)
                if ev.lesson_id in forgotten:
                    out["forgotten"] += 1
                    continue
                newer = conn.execute("SELECT 1 FROM evidence WHERE thread_key = ? AND id != ? AND ts > ?",
                                     (row[2], ev.id, ev.ts)).fetchone()
                if newer:
                    out["stale"] += 1
                    continue
                cur = conn.execute("SELECT * FROM evidence WHERE id = ?", (ev.id,)).fetchone()
                if cur is None:
                    out["new"] += 1
                elif tuple(cur) == row:
                    out["unchanged"] += 1
                    continue
                elif ev.ts < cur["ts"]:
                    out["unchanged"] += 1  # the newest copy wins, as in `dedupe`
                    continue
                else:
                    out["updated"] += 1
                conn.execute("DELETE FROM evidence WHERE thread_key = ? AND id != ?", (row[2], ev.id))
                conn.execute(f"INSERT OR REPLACE INTO evidence ({', '.join(_EVIDENCE_COLS)}) VALUES ({', '.join('?' * len(row))})", row)  # noqa: S608 - fixed column names, values bound
            drop_orphan_vectors(conn)
        return out

    def evidence(self, *, repo: str | None = None, lesson_id: str | None = None) -> list[Evidence]:
        where, args = [], []
        for col, val in (("repo", repo), ("lesson_id", lesson_id)):
            if val is not None:
                where.append(f"{col} = ?")
                args.append(val)
        with self._use(write=False) as conn:
            if not self._ready(conn):
                return []
            rows = conn.execute(f"SELECT * FROM evidence{' WHERE ' + ' AND '.join(where) if where else ''} ORDER BY ts, id", args)  # noqa: S608 - column names are literals, values bound
            return [_evidence_from(r) for r in rows]

    # ---- lessons ----

    def recompute(self, params: Params | None = None, now: float | None = None) -> Recomputed:
        """Replace every lesson with the ones the stored evidence supports at `now`. Counts come from evidence rows only."""
        params = params or Params()
        at = self.now() if now is None else now
        with self._use(write=True) as conn, self._tx(conn):
            lessons = consolidate((_evidence_from(r) for r in conn.execute("SELECT * FROM evidence ORDER BY ts, id")), at, params)
            before = {r["id"]: tuple(r) for r in conn.execute("SELECT * FROM lessons")}
            rows = {le.id: _lesson_row(le) for le in lessons}
            added = sum(1 for k in rows if k not in before)
            changed = sum(1 for k, v in rows.items() if k in before and before[k] != v)
            removed = sum(1 for k in before if k not in rows)
            conn.execute("DELETE FROM lessons")
            conn.executemany(f"INSERT INTO lessons ({', '.join(_LESSON_COLS)}) VALUES ({', '.join('?' * len(_LESSON_COLS))})",  # noqa: S608
                             list(rows.values()))
        return Recomputed(lessons, added, changed, removed)

    def lessons(self, *, host: str | None = None, repo: str | None = None, path_glob: str | None = None,
                language: str | None = None, category: str | None = None, kind: str | None = None,
                status: str | None = None) -> list[Lesson]:
        """Lessons matching every filter given, most supported first."""
        where, args = [], []
        for col, val in (("host", host), ("repo", repo), ("path_glob", path_glob), ("language", language),
                         ("category", category), ("kind", kind), ("status", status)):
            if val is not None:
                where.append(f"{col} = ?")
                args.append(val)
        with self._use(write=False) as conn:
            if not self._ready(conn):
                return []
            rows = conn.execute(
                f"SELECT * FROM lessons{' WHERE ' + ' AND '.join(where) if where else ''} ORDER BY support + contra DESC, id", args)  # noqa: S608 - column names are literals, values bound
            return [_lesson_from(r) for r in rows]

    def put_lessons(self, lessons: Iterable[Lesson]) -> int:
        """Store lessons as given, for seeding an eval or a test. The next `recompute` replaces them with what the evidence says."""
        rows = [_lesson_row(le) for le in lessons]
        with self._use(write=True) as conn, self._tx(conn):
            conn.executemany(f"INSERT OR REPLACE INTO lessons ({', '.join(_LESSON_COLS)}) VALUES ({', '.join('?' * len(_LESSON_COLS))})",  # noqa: S608
                             rows)
        return len(rows)

    def lesson(self, lesson_id: str) -> Lesson | None:
        with self._use(write=False) as conn:
            if not self._ready(conn):
                return None
            row = conn.execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
            return _lesson_from(row) if row else None

    def forget(self, lesson_id: str) -> dict:
        """Delete a lesson and its evidence, and remember the id so a later `consolidate` does not bring it back."""
        with self._use(write=True) as conn, self._tx(conn):
            n = conn.execute("DELETE FROM evidence WHERE lesson_id = ?", (lesson_id,)).rowcount
            gone = conn.execute("DELETE FROM lessons WHERE id = ?", (lesson_id,)).rowcount
            drop_orphan_vectors(conn)
            conn.execute("INSERT OR REPLACE INTO forgotten (lesson_id, ts) VALUES (?, ?)", (lesson_id, self.now()))
        return {"lesson": bool(gone), "evidence": n}

    def delete_evidence_ids(self, ids: Iterable[str]) -> int:
        """Delete evidence rows by id (a server told us they were purged). Returns rows removed; the caller recomputes."""
        wanted = list(dict.fromkeys(ids))
        gone = 0
        with self._use(write=True) as conn, self._tx(conn):
            for i in range(0, len(wanted), 500):
                chunk = wanted[i:i + 500]
                gone += conn.execute(f"DELETE FROM evidence WHERE id IN ({', '.join('?' * len(chunk))})", chunk).rowcount  # noqa: S608 - placeholders only
            drop_orphan_vectors(conn)
        return gone

    def expire(self, params: Params | None = None, now: float | None = None) -> int:
        """Delete evidence older than `expire_days` (it no longer counts for anything) and recompute. Returns rows removed."""
        params = params or Params()
        at = self.now() if now is None else now
        with self._use(write=True) as conn, self._tx(conn):
            n = conn.execute("DELETE FROM evidence WHERE ? - ts >= ?", (at, params.expire_days * DAY)).rowcount
            drop_orphan_vectors(conn)
        self.recompute(params, at)
        return n

    def status(self) -> dict:
        with self._use(write=False) as conn:
            if not self._ready(conn):
                return {"exists": self.exists(), "evidence": 0, "lessons": {}, "schema": 0}
            by_status = Counter(r[0] for r in conn.execute("SELECT status FROM lessons"))
            return {"exists": True, "schema": SCHEMA_VERSION, "evidence": conn.execute("SELECT count(*) FROM evidence").fetchone()[0],
                    "lessons": dict(sorted(by_status.items())),
                    "forgotten": conn.execute("SELECT count(*) FROM forgotten").fetchone()[0]}
