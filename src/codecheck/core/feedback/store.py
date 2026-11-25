"""Pending drafts and the ask cooldown, kept in one `feedback:state` key of the user-level state file."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from codecheck.core.feedback.signals import Signal
from codecheck.core.state import StateStore

KEY = "feedback:state"
ALWAYS_COLLECTED = "user-reported"  # a report the user wrote is kept even after "never ask again"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class FeedbackStore:
    def __init__(self, state: StateStore, now: Callable[[], datetime] = _utc_now) -> None:
        self.state, self.now = state, now

    def _get(self) -> dict:
        return self.state.get(KEY) or {}

    def _stamp(self) -> str:
        return self.now().isoformat()

    def add(self, signal: Signal) -> dict | None:
        """Record the signal; returns its pending entry, or None when "never ask again" is on (user reports excepted)."""
        entry_box: dict = {}

        def update(cur):
            cur = dict(cur or {})
            if cur.get("never") and signal.kind != ALWAYS_COLLECTED:  # checked under the lock: a concurrent "never" wins
                return cur
            pending = dict(cur.get("pending", {}))
            e = dict(pending.get(signal.fingerprint) or {
                "fingerprint": signal.fingerprint, "kind": signal.kind, "source": signal.source,
                "detail": signal.detail, "judged": signal.judged, "count": 0, "first_seen": self._stamp()})
            e["count"] += 1
            e["last_seen"] = self._stamp()
            e["judged"] = bool(e["judged"] and signal.judged)  # one deterministic sighting makes it deterministic
            pending[signal.fingerprint] = e
            cur["pending"] = pending
            entry_box.update(e)
            return cur

        self.state.update(KEY, update)
        return entry_box or None

    def pending(self) -> list[dict]:
        """Deterministic signals first, then the most often seen, then the newest."""
        entries = sorted(self._get().get("pending", {}).values(), key=lambda e: e["last_seen"], reverse=True)
        return sorted(entries, key=lambda e: (e["judged"], -e["count"]))  # stable: equal counts stay newest first

    def get(self, fingerprint: str) -> dict | None:
        return self._get().get("pending", {}).get(fingerprint)

    def remove(self, fingerprint: str) -> None:
        def update(cur):
            cur = dict(cur or {})
            cur["pending"] = {k: v for k, v in cur.get("pending", {}).items() if k != fingerprint}
            return cur
        self.state.update(KEY, update)

    def decide(self, min_interval_hours: int) -> tuple[bool, str]:
        cur = self._get()
        if cur.get("never"):
            return False, "never"
        if not cur.get("pending"):
            return False, "nothing-pending"
        now = self.now()
        until = cur.get("snoozed_until")
        if until and now < datetime.fromisoformat(until):
            return False, "snoozed"
        asked = cur.get("last_asked_at")
        if asked and now - datetime.fromisoformat(asked) < timedelta(hours=min_interval_hours):
            return False, "asked-recently"
        return True, "ok"

    def _set(self, **fields) -> None:
        self.state.update(KEY, lambda cur: {**(cur or {}), **fields})

    def mark_asked(self) -> None:
        self._set(last_asked_at=self._stamp())

    def snooze(self, days: int) -> None:
        self._set(snoozed_until=(self.now() + timedelta(days=days)).isoformat())

    def set_never(self) -> None:
        self._set(never=True, pending={})
