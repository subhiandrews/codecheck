"""Phase 0: would lessons built from earlier runs have applied to later ones? Read-only; nothing is written anywhere.

Temporal leave-one-out: for each run, in order, lessons are built from the evidence of strictly earlier runs only, and
the run's own misses and labelled findings are checked against them. Repo is part of every scope, so counts never mix
repositories.
"""

import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from codecheck.core.panel import detect_language
from codecheck.memory.categories import ANY, canonical_category
from codecheck.memory.consolidate import WEIGHTS, Params, consolidate
from codecheck.memory.model import (
    DEFAULT_HOST,
    Evidence,
    Lesson,
    Scope,
    evidence_id,
    lesson_id,
)
from codecheck.shadow.export import finding_label, run_feedback
from codecheck.shadow.store import ShadowStore

_VERDICT_SIGNALS = {"valid": "verdict_valid", "incorrect": "verdict_incorrect", "preference": "verdict_preference",
                    "out_of_scope": "verdict_out_of_scope"}


@dataclass
class RunData:
    run_id: str
    repo: str
    pr: str
    ts: float
    evidence: list[Evidence] = field(default_factory=list)
    misses: list[Scope] = field(default_factory=list)  # one per unmatched human comment
    labelled: list[tuple[Scope, str]] = field(default_factory=list)  # (scope, confirmed|disputed) per finding
    counts: Counter = field(default_factory=Counter)


def path_glob(path: object) -> str:
    """The first two directories of a path plus `/**`; a file at the top level is `*`. Never absolute, never `..`."""
    if not isinstance(path, str):
        return "*"
    dirs = [p for p in path.replace("\\", "/").split("/")[:-1] if p not in ("", ".", "..")]
    return "/".join(dirs[:2]) + "/**" if dirs else "*"


def scope_for(repo: str, path: object, category: str, host: str = DEFAULT_HOST) -> Scope:
    language = detect_language(path) if isinstance(path, str) else None
    return Scope(host, repo, path_glob(path), language or "", category)


def _evidence(scope: Scope, pr: str, thread_id: str, signal: str, ts: float, version: object) -> Evidence:
    polarity, weight = WEIGHTS[signal]
    kind = "miss" if polarity == "miss" else "confirm" if polarity == "for" else "dispute"
    return Evidence(
        id=evidence_id(scope.host, scope.repo, pr, thread_id, signal), lesson_id=lesson_id(scope, kind), scope=scope,
        polarity=polarity, signal=signal, weight=weight, ts=ts, pr=pr, tool_version=version if isinstance(version, str) else None,
    )


def _outcome_signal(outcome: dict) -> str | None:
    """One signal per thread, the strongest: a verdict, then a resolve, then a bare reply."""
    verdict = _VERDICT_SIGNALS.get(outcome.get("verdict"))
    if verdict:
        return verdict
    resolved, outdated = bool(outcome.get("resolved")), bool(outcome.get("outdated"))
    if resolved and outdated:
        return "resolved_outdated"
    if resolved:
        return "resolved"
    if outdated:
        return "outdated"
    return "reply" if outcome.get("replies") else None


def load_runs(shadow: ShadowStore) -> list[RunData]:
    """Every run that recorded a repo and PR, oldest first (`ordered_run_ids`, never `run_ids`)."""
    feedback = run_feedback(shadow)
    runs = []
    for rid in shadow.ordered_run_ids():
        recs = shadow.read(rid)
        run = next((r for r in recs if r.get("type") == "run"), None)
        if not run or not isinstance(run.get("repo"), str) or not isinstance(run.get("pr"), str):
            continue
        stamps = [r["ts"] for r in recs if r.get("type") == "run" and isinstance(r.get("ts"), (int, float))]
        if not stamps:
            continue
        data = RunData(rid, run["repo"], run["pr"], min(stamps))
        outcomes, humans = feedback.get(rid, ({}, {}))
        version = run.get("tool_version")
        for f in (r for r in recs if r.get("type") == "finding" and isinstance(r.get("id"), str)):
            data.counts["findings"] += 1
            scope = scope_for(data.repo, f.get("file"), canonical_category(f.get("category")))
            outcome = outcomes.get(f["id"])
            signal = _outcome_signal(outcome) if outcome else None
            if outcome and signal:
                data.evidence.append(_evidence(scope, data.pr, str(outcome.get("thread_id")), signal, data.ts, version))
                data.counts[f"signal:{signal}"] += 1
            for h in (h for h in humans.values() if h.get("matches_finding") == f["id"]):
                data.evidence.append(_evidence(scope, data.pr, str(h.get("thread_id")), "human_matched", data.ts, version))
                data.counts["signal:human_matched"] += 1
            label = finding_label(f["id"], outcomes, humans)
            if label in ("confirmed", "disputed"):
                data.labelled.append((scope, label))
        for h in humans.values():
            data.counts["human_comments"] += 1
            if not h.get("matches_finding"):
                scope = scope_for(data.repo, h.get("path"), ANY)
                data.misses.append(scope)
                data.evidence.append(_evidence(scope, data.pr, str(h.get("thread_id")), "human_miss", data.ts, version))
                data.counts["signal:human_miss"] += 1
        runs.append(data)
    return runs


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
    params = params or Params()
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
            if prior and prior.kind in ("confirm", "dispute"):
                bucket["with_prior_lesson"] += 1
                if prior.status == "contested":
                    bucket["prior_contested"] += 1
                elif (prior.kind == "confirm") == (label == "confirmed"):
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
        by_kind = Counter(f"{le.kind}:{le.status}" for le in mine)
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
        "unweighted": True, "note": "roles are not recorded, so no weight reflects who commented; the write-access rule of the activation threshold is not applied",
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

