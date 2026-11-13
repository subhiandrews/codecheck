"""Persistent loop/review state, a cross-process lock, fingerprints and work selection."""

import contextlib
import hashlib
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from codecheck.errors import CodeCheckError
from codecheck.models import Check, PRRef, PullRequest, Thread

# A loop work item that fails this many times for the same fingerprint is marked processed (given up on)
# until something changes on the PR; earlier failures are retried on the next iteration.
MAX_LOOP_FAILURES = 3
_DELETE = object()


def ensure_private_dir(path: Path) -> Path:
    """Create `path` (and parents) if needed; the directory itself is created owner-only (0700)."""
    path = Path(path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileExistsError):
            path.mkdir(mode=0o700)
    return path


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # EPERM: it exists but belongs to someone else
        return True
    return True


class StateStore:
    """JSON key/value state. Every write is a read-modify-write under a short cross-process lock."""

    def __init__(self, path: Path, *, lock_timeout: float = 10.0) -> None:
        self.path = Path(path)
        self.lock_timeout = lock_timeout

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            with contextlib.suppress(OSError):
                os.replace(self.path, self.path.with_suffix(".corrupt"))
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict) -> None:
        ensure_private_dir(self.path.parent)
        tmp = self.path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        ensure_private_dir(self.path.parent)
        lock = FileLock(self.path.with_name(self.path.name + ".lock"), stale_after=60)
        if not lock.acquire_wait(self.lock_timeout):
            raise CodeCheckError(f"state file {self.path} is locked by another process; try again")
        try:
            yield
        finally:
            lock.release()

    def get(self, key: str, default=None):
        return self._load().get(key, default)

    def update(self, key: str, fn: Callable):
        """Atomically replace `key` with fn(current value or None); fn may return `_DELETE` to remove it."""
        with self._locked():
            data = self._load()
            value = fn(data.get(key))
            if value is _DELETE:
                if key not in data:
                    return None
                data.pop(key)
                value = None
            else:
                data[key] = value
            self._write(data)
            return value

    def set(self, key: str, value) -> None:
        self.update(key, lambda _old: value)

    def delete(self, key: str) -> None:
        self.update(key, lambda _old: _DELETE)

    def incr(self, key: str) -> int:
        return self.update(key, lambda old: int(old or 0) + 1)


class FileLock:
    """Exclusive lock file holding "<pid>:<uuid>".

    Another process may reclaim it when the holder's pid is no longer alive (same host), or when the file
    has not been touched for `stale_after` seconds. Long holders call `touch()` (or `keep_alive`) to stay fresh.
    """

    def __init__(self, path: Path, *, stale_after: float = 7200, now: Callable[[], float] = time.time,
                 alive: Callable[[int], bool] = pid_alive) -> None:
        self.path, self.stale_after, self.now, self.alive = Path(path), stale_after, now, alive
        self._token: str | None = None

    def _create(self) -> bool:
        token = f"{os.getpid()}:{uuid.uuid4().hex}"
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        try:
            os.write(fd, token.encode())
        except BaseException:
            os.close(fd)
            self.path.unlink(missing_ok=True)
            raise
        os.close(fd)
        self._token = token
        return True

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._create():
            return True
        try:
            seen = self.path.stat()
        except FileNotFoundError:
            return self._create()
        holder = self._holder_pid()
        holder_dead = holder is not None and holder != os.getpid() and not self.alive(holder)
        if not holder_dead and self.now() - seen.st_mtime < self.stale_after:
            return False
        # Reclaim: move the lock aside atomically, then confirm it is the same stale file we judged.
        aside = self.path.with_name(f"{self.path.name}.reclaim.{uuid.uuid4().hex}")
        try:
            os.rename(self.path, aside)
        except FileNotFoundError:
            return self._create()
        try:
            moved = aside.stat()
            if (moved.st_ino, moved.st_mtime_ns) == (seen.st_ino, seen.st_mtime_ns):
                return self._create()
            # We grabbed a lock that was freshly re-acquired; put it back (fails if someone else created one).
            with contextlib.suppress(OSError):
                os.link(aside, self.path)
            return False
        finally:
            aside.unlink(missing_ok=True)

    def _holder_pid(self) -> int | None:
        try:
            pid = self.path.read_text(encoding="utf-8").split(":", 1)[0]
        except (OSError, UnicodeDecodeError):
            return None
        return int(pid) if pid.isdigit() else None

    def acquire_wait(self, timeout: float, poll: float = 0.02) -> bool:
        end = time.monotonic() + timeout
        while True:
            if self.acquire():
                return True
            if time.monotonic() >= end:
                return False
            time.sleep(poll)

    def touch(self) -> bool:
        """Refresh the lock's mtime if we still hold it; False if it is no longer ours."""
        token = self._token
        if token is None:
            return False
        try:
            if self.path.read_text(encoding="utf-8") != token:
                return False
            os.utime(self.path)
        except (OSError, UnicodeDecodeError):
            return False
        return True

    @contextlib.contextmanager
    def keep_alive(self, interval: float = 60.0) -> Iterator[None]:
        """Touch the lock every `interval` seconds from a background thread while the block runs."""
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(interval):
                self.touch()

        thread = threading.Thread(target=beat, name="code-check-lock-heartbeat", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=5)

    def release(self) -> None:
        token, self._token = self._token, None
        if token is None:
            return
        try:
            if self.path.read_text(encoding="utf-8") == token:
                self.path.unlink(missing_ok=True)
        except (FileNotFoundError, UnicodeDecodeError):
            pass


def local_fingerprint(merge_base: str, head: str, diff: str) -> str:
    return hashlib.sha256(f"{merge_base}\0{head}\0{diff}".encode()).hexdigest()[:16]


def actionable_threads(threads: Sequence[Thread], me: str) -> list[Thread]:
    return [
        t for t in threads
        if not t.is_resolved and not t.is_outdated and t.comments and t.comments[-1].author != me
    ]


@dataclass(frozen=True, slots=True)
class WorkItem:
    ref: PRRef
    fingerprint: str
    reason: str


def babysit_work(pr: PullRequest, threads: Sequence[Thread], checks: Sequence[Check], me: str) -> WorkItem | None:
    if pr.state != "open":
        return None
    actionable = actionable_threads(threads, me)
    failing = [c for c in checks if c.state == "fail"]
    if not actionable and not failing:
        return None
    parts = [
        pr.head_sha,
        *sorted(f"{t.id}:{t.comments[-1].id}" for t in actionable),
        *sorted(f"{c.name}={c.state}" for c in checks),
    ]
    fp = hashlib.sha256("\0".join(parts).encode()).hexdigest()[:16]
    bits = []
    if actionable:
        bits.append(f"{len(actionable)} thread{'s' if len(actionable) != 1 else ''}")
    if failing:
        bits.append(f"{len(failing)} failing check{'s' if len(failing) != 1 else ''}")
    return WorkItem(pr.ref, fp, ", ".join(bits))


def loop_key(mode: str, ref: PRRef) -> str:
    """Loop bookkeeping key; deliberately separate from `review:<ref>` (which means "this head was reviewed")."""
    return f"loop:{mode}:{ref}"


def select_work(items: Sequence[WorkItem], state: StateStore, mode: str, limit: int) -> list[WorkItem]:
    fresh = [i for i in items if state.get(loop_key(mode, i.ref)) != i.fingerprint]
    return fresh[:limit]


def mark_processed(state: StateStore, mode: str, item: WorkItem) -> None:
    state.set(loop_key(mode, item.ref), item.fingerprint)
    state.delete(f"{loop_key(mode, item.ref)}:failures")


def record_failure(state: StateStore, mode: str, item: WorkItem, limit: int = MAX_LOOP_FAILURES) -> tuple[int, bool]:
    """Count a failed attempt at this fingerprint. Returns (count, gave_up); after `limit` failures the item
    is marked processed so a PR that always fails cannot starve the queue."""

    def bump(old):
        count = old["count"] if isinstance(old, dict) and old.get("fingerprint") == item.fingerprint else 0
        return {"fingerprint": item.fingerprint, "count": int(count) + 1}

    count = state.update(f"{loop_key(mode, item.ref)}:failures", bump)["count"]
    if count >= limit:
        mark_processed(state, mode, item)
        return count, True
    return count, False
