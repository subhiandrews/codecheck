"""Persistent loop/review state, a cross-process lock, fingerprints and work selection."""

import contextlib
import hashlib
import json
import os
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from codecheck.models import Check, PRRef, PullRequest, Thread


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, key: str, default=None):
        return self._load().get(key, default)

    def set(self, key: str, value) -> None:
        data = self._load()
        data[key] = value
        self._write(data)

    def delete(self, key: str) -> None:
        data = self._load()
        if data.pop(key, None) is not None:
            self._write(data)

    def incr(self, key: str) -> int:
        data = self._load()
        data[key] = int(data.get(key, 0)) + 1
        self._write(data)
        return data[key]


class FileLock:
    def __init__(self, path: Path, *, stale_after: float = 7200, now: Callable[[], float] = time.time) -> None:
        self.path, self.stale_after, self.now = Path(path), stale_after, now
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
        if self.now() - seen.st_mtime < self.stale_after:
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


def select_work(items: Sequence[WorkItem], state: StateStore, mode: str, limit: int) -> list[WorkItem]:
    fresh = [i for i in items if state.get(f"{mode}:{i.ref}") != i.fingerprint]
    return fresh[:limit]


def mark_processed(state: StateStore, mode: str, item: WorkItem) -> None:
    state.set(f"{mode}:{item.ref}", item.fingerprint)
