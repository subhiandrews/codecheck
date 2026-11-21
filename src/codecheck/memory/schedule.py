"""Which reviewed PRs are due for another outcome collection. Pure decisions plus bookkeeping in the `StateStore`.

A review is collected 1, 3, 7 and 14 days after it was posted, and no more once the PR has been closed for 7 days. The
host reports a PR as closed or merged but not when, so the first time a collection sees it closed is its closing time.
Bookkeeping lives under `memory:collect:<ref>` keys; the `review:<ref>` and `loop:<mode>:<ref>` keys belong to other commands.
"""

from collections.abc import Callable
from dataclasses import dataclass

from codecheck.core.state import StateStore
from codecheck.shadow.store import ShadowStore

DAY = 86400.0
MILESTONES_DAYS = (1, 3, 7, 14)
CLOSED_GRACE_DAYS = 7
MAX_PRS = 3
BUDGET_SECONDS = 10.0
PREFIX = "memory:collect:"


def state_key(pr: str) -> str:
    return f"{PREFIX}{pr}"


@dataclass(frozen=True)
class Candidate:
    pr: str
    posted: float
    milestone: int  # index into MILESTONES_DAYS of the newest milestone that has passed; -1 with --all and none passed


def posted_times(shadow: ShadowStore) -> dict[str, float]:
    """PR ref -> when its newest review was posted: the `decision` record's time, else the `run` record's."""
    out: dict[str, float] = {}
    for rid in shadow.ordered_run_ids():
        recs = shadow.read(rid)
        run = next((r for r in recs if r.get("type") == "run"), None)
        if not run or not isinstance(run.get("pr"), str) or not run["pr"]:
            continue
        stamps = [r["ts"] for r in recs if r.get("type") == "decision" and isinstance(r.get("ts"), (int, float))]
        stamps = stamps or [r["ts"] for r in recs if r.get("type") == "run" and isinstance(r.get("ts"), (int, float))]
        if stamps:
            out[run["pr"]] = max(out.get(run["pr"], 0.0), max(stamps))
    return out


def milestone_passed(posted: float, now: float) -> int:
    passed = [i for i, d in enumerate(MILESTONES_DAYS) if now - posted >= d * DAY]
    return passed[-1] if passed else -1


def _entry(state: StateStore, pr: str, posted: float) -> dict:
    entry = state.get(state_key(pr))
    if not isinstance(entry, dict) or entry.get("posted") != posted:  # a newer review restarts the schedule
        return {"posted": posted, "done": -1, "closed_at": None}
    return entry


def due(state: StateStore, shadow: ShadowStore, now: float, *, everything: bool = False) -> list[Candidate]:
    """PRs due now, newest review first. `everything` ignores the schedule (not the closed-PR cutoff's bookkeeping)."""
    found = []
    for pr, posted in posted_times(shadow).items():
        entry = _entry(state, pr, posted)
        passed = milestone_passed(posted, now)
        closed_at = entry.get("closed_at")
        if isinstance(closed_at, (int, float)) and now - closed_at >= CLOSED_GRACE_DAYS * DAY and not everything:
            continue
        if everything or passed > entry.get("done", -1):
            found.append(Candidate(pr, posted, passed))
    return sorted(found, key=lambda c: -c.posted)


def mark_collected(state: StateStore, cand: Candidate, now: float, *, pr_state: str | None = None) -> None:
    """Record that `cand` was collected, and the PR's closing time the first time it is seen closed."""
    def update(old):
        entry = _entry_from(old, cand.posted)
        entry["done"] = max(entry["done"], cand.milestone)
        if pr_state in ("closed", "merged") and entry.get("closed_at") is None:
            entry["closed_at"] = now
        return entry
    state.update(state_key(cand.pr), update)


def _entry_from(old, posted: float) -> dict:
    if not isinstance(old, dict) or old.get("posted") != posted:
        return {"posted": posted, "done": -1, "closed_at": None}
    return dict(old)


def run_budgeted(cands: list[Candidate], work: Callable[[Candidate], None], mono: Callable[[], float],
                 *, max_prs: int = MAX_PRS, budget: float = BUDGET_SECONDS) -> list[Candidate]:
    """Call `work` for at most `max_prs` candidates, stopping once `budget` seconds have passed. Returns those attempted.
    A call in progress is not interrupted, so the budget is checked between PRs."""
    start, attempted = mono(), []
    for cand in cands[:max_prs]:
        if attempted and mono() - start >= budget:
            break
        attempted.append(cand)
        work(cand)
    return attempted
