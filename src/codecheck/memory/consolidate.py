"""Evidence to lessons: counting only, no model call. Pure and deterministic; the clock and every limit are arguments.

Evidence is grouped by scope. A group of misses is one `miss` lesson. A group of findings with outcomes is one lesson of
the leading side (`confirm`, or `dispute`; a tie is a dispute); the other side is its `contra` weight.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from codecheck.memory.model import Evidence, Lesson, Scope, lesson_id

DAY = 86400.0

# Signal -> (polarity, weight). Roles are not recorded yet, so every weight is the unweighted one, and lessons say so.
# Reply alone is neutral (decision 0009). The dispute-from-the-PR-author weight (0) and the 0.5 for a self-dispute
# need roles; until they exist an `against` verdict counts in full.
WEIGHTS: dict[str, tuple[str, float]] = {
    "verdict_valid": ("for", 1.0),
    "verdict_incorrect": ("against", 1.0),
    "verdict_preference": ("against", 1.0),
    "verdict_out_of_scope": ("against", 1.0),
    "resolved_outdated": ("for", 1.0),
    "resolved": ("for", 0.25),
    "outdated": ("for", 0.25),
    "human_matched": ("for", 1.0),
    "human_miss": ("miss", 1.0),
    "reply": ("neutral", 0.0),
}


@dataclass(frozen=True)
class Params:
    activation_weight: float = 2.0
    min_prs: int = 2
    contest_ratio: float = 0.5
    half_days: float = 90.0
    expire_days: float = 180.0


def effective_weight(ev: Evidence, now: float, params: Params) -> float:
    """Weight after decay at `now`. Evidence from after `now` does not exist yet and counts for nothing."""
    age = (now - ev.ts) / DAY
    if age < 0 or age >= params.expire_days:
        return 0.0
    return ev.weight * (0.5 if age >= params.half_days else 1.0)


def dedupe(evidence: Iterable[Evidence]) -> list[Evidence]:
    """One row per evidence id (the newest), so collecting a thread twice never counts twice."""
    best: dict[str, Evidence] = {}
    for ev in evidence:
        cur = best.get(ev.id)
        if cur is None or ev.ts >= cur.ts:
            best[ev.id] = ev
    return sorted(best.values(), key=lambda e: (e.ts, e.id))


def _statement(kind: str, scope: Scope, support: float, contra: float, n_prs: int) -> str:
    where = f"{scope.repo} under {scope.path_glob}" + (f" ({scope.language})" if scope.language else "")
    what = {"miss": "a human reviewer raised a point we missed", "dispute": f"{scope.category} findings were disputed",
            "confirm": f"{scope.category} findings were confirmed"}[kind]
    return f"In {where}, {what}: weight {support:g} against {contra:g} across {n_prs} PR(s)."


def _lesson(scope: Scope, items: list[Evidence], now: float, params: Params) -> Lesson:
    first_ts = min(e.ts for e in items)
    last_ts = max(e.ts for e in items)
    live = [(e, effective_weight(e, now, params)) for e in items if e.polarity in ("for", "against", "miss")]
    live = [(e, w) for e, w in live if w > 0]
    for_w = sum(w for e, w in live if e.polarity == "for")
    against_w = sum(w for e, w in live if e.polarity == "against")
    miss_w = sum(w for e, w in live if e.polarity == "miss")
    if miss_w:
        kind, side, support, contra = "miss", "miss", miss_w, 0.0
    elif for_w > against_w:
        kind, side, support, contra = "confirm", "for", for_w, against_w
    else:
        kind, side, support, contra = "dispute", "against", against_w, for_w
    n_prs = len({e.pr for e, _ in live if e.polarity == side})
    if not live:
        status = "expired"
    elif contra > 0 and contra >= params.contest_ratio * support:
        status = "contested"
    elif support >= params.activation_weight and n_prs >= params.min_prs:
        status = "active"
    else:
        status = "candidate"
    total = support + contra
    return Lesson(
        id=lesson_id(scope, kind), scope=scope, kind=kind, statement=_statement(kind, scope, support, contra, n_prs),
        support=round(support, 4), contra=round(contra, 4), n_prs=n_prs,
        confidence=round(support / total, 4) if total else 0.0, status=status, first_ts=first_ts, last_ts=last_ts,
        expires_ts=last_ts + params.expire_days * DAY, unweighted=True,
        provenance={"evidence": len(items), "live": len(live), "prs": sorted({e.pr for e, _ in live if e.polarity == side})},
    )


def consolidate(evidence: Iterable[Evidence], now: float, params: Params | None = None) -> list[Lesson]:
    """Lessons from the evidence that exists at `now`, sorted by id. Evidence dated after `now` is ignored."""
    params = params or Params()
    groups: dict[tuple, list[Evidence]] = {}
    for ev in dedupe(evidence):
        if ev.ts <= now:
            groups.setdefault(ev.scope.key(), []).append(ev)
    lessons = [_lesson(items[0].scope, items, now, params) for items in groups.values()
               if any(e.polarity != "neutral" for e in items)]  # a reply alone teaches nothing
    return sorted(lessons, key=lambda lesson: lesson.id)
