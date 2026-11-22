"""Evidence to lessons: counting only, no model call. Pure and deterministic; the clock and every limit are arguments.

Evidence is grouped by scope. A group of misses is one `miss` lesson. Every other group is one `finding` lesson that
carries a `support` weight (confirming) and a `contra` weight (disputing); which way it leans is derived for display and
is not part of the id. The weight of an item comes from its signal and the role of the person behind it, recomputed here
every time: a stored or client-sent weight is never trusted.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from codecheck.memory.model import UNKNOWN, Evidence, Lesson, Scope, lesson_id

DAY = 86400.0

# Signal -> polarity. Reply alone is neutral (decision 0009).
POLARITY: dict[str, str] = {
    "verdict_valid": "for", "verdict_incorrect": "against", "verdict_preference": "against",
    "verdict_out_of_scope": "against", "resolved_outdated": "for", "resolved": "for", "outdated": "for",
    "human_matched": "for", "human_miss": "miss", "reply": "neutral",
}
# What a dispute counts for, by who made it. A PR author's word on their own PR is not independent, so it counts for
# nothing; `unknown` (evidence recorded before roles existed) is treated like a contributor, the middle value.
AGAINST_BY_ROLE = {"maintainer": 1.0, "contributor": 0.5, "self": 0.5, "author": 0.0, UNKNOWN: 0.5}
# What a missed human comment counts for, by who wrote it.
MISS_BY_ROLE = {"maintainer": 1.0, "contributor": 0.5, "author": 0.25, "self": 0.25, UNKNOWN: 0.5}
# Everything else does not depend on a role.
FLAT_FOR = {"verdict_valid": 1.0, "resolved_outdated": 1.0, "resolved": 0.25, "outdated": 0.25, "human_matched": 1.0}
# The phase 0 weights: nobody's role is known, so every item counts in full (`memory measure` is unweighted).
UNWEIGHTED = {"verdict_valid": 1.0, "verdict_incorrect": 1.0, "verdict_preference": 1.0, "verdict_out_of_scope": 1.0,
              "resolved_outdated": 1.0, "resolved": 0.25, "outdated": 0.25, "human_matched": 1.0, "human_miss": 1.0,
              "reply": 0.0}


@dataclass(frozen=True)
class Params:
    activation_weight: float = 2.0
    min_prs: int = 2
    contest_ratio: float = 0.5
    half_days: float = 90.0
    expire_days: float = 180.0
    weighted: bool = True  # False: the phase 0 flat weights and no maintainer requirement


def polarity(signal: str) -> str:
    return POLARITY.get(signal, "neutral")


def base_weight(signal: str, role: str, weighted: bool = True) -> float:
    """The weight of one item before decay. Unknown signals count for nothing."""
    if signal not in POLARITY:
        return 0.0
    if not weighted:
        return UNWEIGHTED[signal]
    pol = POLARITY[signal]
    if pol == "against":
        return AGAINST_BY_ROLE.get(role, AGAINST_BY_ROLE[UNKNOWN])
    if pol == "miss":
        return MISS_BY_ROLE.get(role, MISS_BY_ROLE[UNKNOWN])
    return FLAT_FOR.get(signal, 0.0)


def effective_weight(ev: Evidence, now: float, params: Params) -> float:
    """Weight after decay at `now`. Evidence from after `now` does not exist yet and counts for nothing."""
    age = (now - ev.ts) / DAY
    if age < 0 or age >= params.expire_days:
        return 0.0
    return base_weight(ev.signal, ev.role, params.weighted) * (0.5 if age >= params.half_days else 1.0)


def dedupe(evidence: Iterable[Evidence]) -> list[Evidence]:
    """One row per evidence id (the newest), so collecting a thread twice never counts twice."""
    best: dict[str, Evidence] = {}
    for ev in evidence:
        cur = best.get(ev.id)
        if cur is None or ev.ts >= cur.ts:
            best[ev.id] = ev
    return sorted(best.values(), key=lambda e: (e.ts, e.id))


def _statement(kind: str, lean: str, scope: Scope, support: float, contra: float, n_prs: int) -> str:
    where = f"{scope.repo} under {scope.path_glob}" + (f" ({scope.language})" if scope.language else "")
    if kind == "miss":
        return f"In {where}, a human reviewer raised {support:g} weight of points we missed across {n_prs} PR(s)."
    return (f"In {where}, {scope.category} findings: weight {support:g} confirming against {contra:g} disputing "
            f"across {n_prs} PR(s); leans {lean}.")


def _lesson(scope: Scope, items: list[Evidence], now: float, params: Params) -> Lesson:
    first_ts = min(e.ts for e in items)
    last_ts = max(e.ts for e in items)
    live = [(e, effective_weight(e, now, params)) for e in items if polarity(e.signal) in ("for", "against", "miss")]
    live = [(e, w) for e, w in live if w > 0]
    for_w = sum(w for e, w in live if polarity(e.signal) == "for")
    against_w = sum(w for e, w in live if polarity(e.signal) == "against")
    miss_w = sum(w for e, w in live if polarity(e.signal) == "miss")
    if miss_w or (not live and any(polarity(e.signal) == "miss" for e in items)):
        kind, lean, side, support, contra = "miss", "", "miss", miss_w, 0.0
    elif for_w > against_w:
        kind, lean, side, support, contra = "finding", "confirm", "for", for_w, against_w
    else:
        kind, lean, side, support, contra = "finding", "dispute", "against", for_w, against_w
    lead, minor = (support, contra) if lean != "dispute" else (contra, support)
    on_side = [e for e, _ in live if polarity(e.signal) == side]
    n_prs = len({e.pr for e in on_side})
    maintainer = any(e.role == "maintainer" for e in on_side)
    if not live:
        status = "expired"
    elif minor > 0 and minor >= params.contest_ratio * lead:
        status = "contested"
    elif lead >= params.activation_weight and n_prs >= params.min_prs and (maintainer or not params.weighted):
        status = "active"
    else:
        status = "candidate"
    total = support + contra
    return Lesson(
        id=lesson_id(scope, kind), scope=scope, kind=kind, lean=lean,
        statement=_statement(kind, lean, scope, support, contra, n_prs),
        support=round(support, 4), contra=round(contra, 4), n_prs=n_prs,
        confidence=round(lead / total, 4) if total else 0.0, status=status, first_ts=first_ts, last_ts=last_ts,
        expires_ts=last_ts + params.expire_days * DAY, maintainer=maintainer,
        provenance={"evidence": len(items), "live": len(live), "prs": sorted({e.pr for e in on_side}),
                    "roles": sorted({e.role for e in on_side})},
    )


def consolidate(evidence: Iterable[Evidence], now: float, params: Params | None = None) -> list[Lesson]:
    """Lessons from the evidence that exists at `now`, sorted by id. Evidence dated after `now` is ignored."""
    params = params or Params()
    groups: dict[tuple, list[Evidence]] = {}
    for ev in dedupe(evidence):
        if ev.ts <= now:
            groups.setdefault(ev.scope.key(), []).append(ev)
    # a group that can never carry weight (replies, a PR author's own disputes) teaches nothing and makes no lesson
    lessons = [_lesson(items[0].scope, items, now, params) for items in groups.values()
               if any(base_weight(e.signal, e.role, params.weighted) > 0 for e in items)]
    return sorted(lessons, key=lambda lesson: lesson.id)
