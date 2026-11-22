"""Phase 0: would lessons built from earlier runs have applied to later ones? Read-only; nothing is written anywhere.

Temporal leave-one-out: for each run, in order, lessons are built from the evidence of strictly earlier runs only, and
the run's own misses and labelled findings are checked against them. Repo is part of every scope, so counts never mix
repositories.
"""

import time
from collections import Counter
from collections.abc import Callable

from codecheck.memory.consolidate import Params, consolidate
from codecheck.memory.evidence import RunData, load_runs
from codecheck.memory.model import Lesson
from codecheck.shadow.store import ShadowStore


def lessons_visible_to(runs: list[RunData], index: int, params: Params) -> dict[tuple, Lesson]:
    """The lessons the run at `index` could have had: its predecessors' evidence only, decayed to its own time."""
    earlier = [e for r in runs[:index] for e in r.evidence]
    return {lesson.scope.key(): lesson for lesson in consolidate(earlier, runs[index].ts, params) if lesson.status != "expired"}


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 3) if d else None


def _new_repo() -> dict:
    return {"runs": 0, "prs": set(), "findings": 0, "human_comments": 0, "misses": 0, "misses_with_prior_miss": 0,
            "misses_with_prior_miss_other_pr": 0, "misses_with_any_prior_miss_in_repo": 0,
            "confirmed": Counter(), "disputed": Counter(), "signals": Counter()}


def measure(shadow: ShadowStore, params: Params | None = None, now: Callable[[], float] = time.time) -> dict:
    params = params or Params(weighted=False)  # phase 0 asks about recurrence, so nobody's role changes a weight
    runs = load_runs(shadow)
    repos: dict[str, dict] = {}
    for i, run in enumerate(runs):
        agg = repos.setdefault(run.repo, _new_repo())
        agg["runs"] += 1
        agg["prs"].add(run.pr)
        agg["findings"] += run.counts["findings"]
        agg["human_comments"] += run.counts["human_comments"]
        agg["misses"] += len(run.misses)
        for k, v in run.counts.items():
            if k.startswith("signal:"):
                agg["signals"][k[7:]] += v
        visible = lessons_visible_to(runs, i, params)
        in_repo_miss = any(le.kind == "miss" and le.scope.repo == run.repo for le in visible.values())
        for scope in run.misses:
            prior = visible.get(scope.key())
            if prior and prior.kind == "miss":
                agg["misses_with_prior_miss"] += 1
                if any(p != run.pr for p in prior.provenance.get("prs", [])):
                    agg["misses_with_prior_miss_other_pr"] += 1
            if in_repo_miss:
                agg["misses_with_any_prior_miss_in_repo"] += 1
        for scope, label in run.labelled:
            bucket = agg[label]
            bucket["total"] += 1
            prior = visible.get(scope.key())
            if prior and prior.kind == "finding":
                bucket["with_prior_lesson"] += 1
                if prior.status == "contested":
                    bucket["prior_contested"] += 1
                elif (prior.lean == "confirm") == (label == "confirmed"):
                    bucket["prior_agreed"] += 1
                else:
                    bucket["prior_disagreed"] += 1
    final_at = now()
    final = consolidate([e for r in runs for e in r.evidence], final_at, params)
    per_repo = {}
    for repo in sorted(repos):
        agg = repos[repo]
        mine = [le for le in final if le.scope.repo == repo]
        status = Counter(le.status for le in mine)
        by_kind = Counter(f"{le.lean or le.kind}:{le.status}" for le in mine)
        per_repo[repo] = {
            "runs": agg["runs"], "prs": len(agg["prs"]), "findings": agg["findings"], "human_comments": agg["human_comments"],
            "evidence_by_signal": dict(sorted(agg["signals"].items())),
            "missed_human_comments": agg["misses"],
            "missed_in_bucket_with_earlier_miss": agg["misses_with_prior_miss"],
            "missed_in_bucket_with_earlier_miss_other_pr": agg["misses_with_prior_miss_other_pr"],
            "missed_with_any_earlier_miss_in_repo": agg["misses_with_any_prior_miss_in_repo"],
            "miss_recurrence_rate": _rate(agg["misses_with_prior_miss"], agg["misses"]),
            "confirmed_findings": dict(agg["confirmed"]), "disputed_findings": dict(agg["disputed"]),
            "buckets": {"total": len(mine), **dict(sorted(status.items())), "by_kind_status": dict(sorted(by_kind.items()))},
            "unweighted": True,
        }
    def total(key: str) -> int:
        return sum(r[key] for r in per_repo.values())

    misses, recur = total("missed_human_comments"), total("missed_in_bucket_with_earlier_miss")
    return {
        "unweighted": True, "note": "measured with flat weights: no weight reflects who commented and the maintainer rule of the activation threshold is not applied (`memory consolidate` applies both)",
        "params": {"activation_weight": params.activation_weight, "min_prs": params.min_prs, "contest_ratio": params.contest_ratio,
                   "half_days": params.half_days, "expire_days": params.expire_days},
        "store": {"runs_in_store": len(shadow.run_ids()), "runs_measured": len(runs), "prs": len({(r.repo, r.pr) for r in runs}),
                  "repos": len(per_repo), "findings": total("findings"), "human_comments": total("human_comments"),
                  "evidence": sum(len(r.evidence) for r in runs)},
        "summary": {"missed_human_comments": misses, "missed_in_bucket_with_earlier_miss": recur,
                    "miss_recurrence_rate": _rate(recur, misses),
                    "active_buckets": sum(1 for le in final if le.status == "active"),
                    "contested_buckets": sum(1 for le in final if le.status == "contested")},
        "repos": per_repo,
    }

