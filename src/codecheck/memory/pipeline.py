"""`memory consolidate`: shadow store -> evidence rows -> lessons. Reads the shadow store, writes only the memory store."""

from collections import Counter

from codecheck.memory.consolidate import DAY, Params
from codecheck.memory.db import MemoryDB
from codecheck.memory.evidence import load_runs
from codecheck.shadow.store import ShadowStore


def consolidate_store(shadow: ShadowStore, db: MemoryDB, params: Params, now: float, *, prune: bool = False) -> dict:
    """Idempotent: running it twice on the same stores changes nothing. Evidence that already counts for nothing (older than
    `expire_days`) is not stored, and neither is evidence of a lesson that was forgotten."""
    runs = load_runs(shadow)
    items = [e for r in runs for e in r.evidence]
    live = [e for e in items if (now - e.ts) / DAY < params.expire_days]
    counts: Counter = Counter(skipped_expired=len(items) - len(live))
    if not live and not db.exists():
        return {"store": str(db.file), "runs": len(runs), "evidence": {"new": 0, "updated": 0, "unchanged": 0, **counts},
                "lessons": {"total": 0, "by_status": {}, "by_kind": {}, "added": 0, "changed": 0, "removed": 0}, "pruned": 0}
    counts.update(db.upsert_evidence(live))
    pruned = db.expire(params, now) if prune else 0
    result = db.recompute(params, now)
    lessons = result.lessons
    return {
        "store": str(db.file), "runs": len(runs),
        "evidence": {"new": counts["new"], "updated": counts["updated"], "unchanged": counts["unchanged"],
                     "stale": counts["stale"], "forgotten": counts["forgotten"], "skipped_expired": counts["skipped_expired"]},
        "lessons": {"total": len(lessons), "by_status": dict(sorted(Counter(le.status for le in lessons).items())),
                    "by_kind": dict(sorted(Counter(f"{le.lean or le.kind}" for le in lessons).items())),
                    "added": result.added, "changed": result.changed, "removed": result.removed},
        "pruned": pruned,
    }
