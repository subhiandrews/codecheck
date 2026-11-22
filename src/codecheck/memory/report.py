"""`memory report`: did findings fare differently when lessons were shown? Read-only over the shadow store.

A run that read memory carries a `memory` record (`{"type": "memory", "stage", "ids"}`, written by `ingest` for the
aggregator stage) and its run record carries `memory.read` for the reviewers stage. Each non-blind run's findings are put in
the "with lessons" or the "without" arm and joined with the outcomes collected later (`shadow.export.finding_label`):
confirmed, disputed, replied, no signal. Per repository: counts and the dispute and confirm rates of each arm.

This is observational, not an experiment: runs were not assigned to an arm at random, so a gap can come from the
PRs rather than from memory (use `eval --memory-effect` for the paired answer). An arm with fewer than `MIN_FINDINGS`
findings is flagged `underpowered`, and nothing here changes any setting. Advisory only (decisions 0023, 0044).
"""

from collections import Counter

from codecheck.shadow.export import finding_label, run_feedback
from codecheck.shadow.store import ShadowStore

MIN_FINDINGS = 30  # findings per arm below which a comparison is flagged underpowered; `--min-findings` overrides
ARMS = ("with_lessons", "without_lessons")
NOTE = ("Observational: runs were not assigned to an arm at random, so a gap can come from the pull requests rather than from "
        "memory. Blind runs (which never read memory) are left out of both arms. `eval --memory-effect` is the paired test.")


def add_arguments(p) -> None:
    p.add_argument("--min-findings", type=int, default=MIN_FINDINGS, metavar="N",
                   help=f"findings per arm below which a repository is flagged underpowered (default {MIN_FINDINGS})")


def _read_memory(records: list[dict], run: dict) -> bool:
    meta = run.get("memory")
    return (isinstance(meta, dict) and bool(meta.get("read"))) or any(r.get("type") == "memory" for r in records)


def _lesson_ids(records: list[dict]) -> set[str]:
    return {i for r in records if r.get("type") == "memory" for i in (r.get("ids") or []) if isinstance(i, str)}


def _arm() -> dict:
    return {"runs": 0, "findings": 0, "labels": Counter()}


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 3) if d else None


def _summary(arm: dict) -> dict:
    n, labels = arm["findings"], arm["labels"]
    decided = labels["confirmed"] + labels["disputed"]
    return {"runs": arm["runs"], "findings": n, "confirmed": labels["confirmed"], "disputed": labels["disputed"],
            "replied": labels["replied"], "no_signal": labels["no_signal"] + labels["unreviewed"],
            "dispute_rate": _rate(labels["disputed"], n), "confirm_rate": _rate(labels["confirmed"], n),
            "dispute_share_of_decided": _rate(labels["disputed"], decided)}


def report(shadow: ShadowStore, min_findings: int = MIN_FINDINGS) -> dict:
    feedback = run_feedback(shadow)
    repos: dict[str, dict] = {}
    skipped_blind = 0
    for rid in shadow.ordered_run_ids():
        recs = shadow.read(rid)
        run = next((r for r in recs if r.get("type") == "run"), None)
        if run is None or not isinstance(run.get("repo"), str):
            continue
        if run.get("blind"):
            skipped_blind += 1
            continue
        entry = repos.setdefault(run["repo"], {"arms": {a: _arm() for a in ARMS}, "lessons": set()})
        arm_name = ARMS[0] if _read_memory(recs, run) else ARMS[1]
        arm = entry["arms"][arm_name]
        entry["lessons"] |= _lesson_ids(recs)
        outcomes, humans = feedback.get(rid, ({}, {}))
        arm["runs"] += 1
        for f in (r for r in recs if r.get("type") == "finding" and isinstance(r.get("id"), str)):
            arm["findings"] += 1
            arm["labels"][finding_label(f["id"], outcomes, humans)] += 1
    per_repo, totals = {}, {a: _arm() for a in ARMS}
    for repo in sorted(repos):
        arms = repos[repo]["arms"]
        for name in ARMS:
            totals[name]["runs"] += arms[name]["runs"]
            totals[name]["findings"] += arms[name]["findings"]
            totals[name]["labels"] += arms[name]["labels"]
        per_repo[repo] = {**{name: _summary(arms[name]) for name in ARMS},
                          "lessons_shown": len(repos[repo]["lessons"]),
                          "underpowered": any(arms[name]["findings"] < min_findings for name in ARMS)}
    overall = {name: _summary(totals[name]) for name in ARMS}
    return {"advisory": True, "min_findings": min_findings, "blind_runs_left_out": skipped_blind, "note": NOTE,
            "overall": {**overall, "underpowered": any(totals[name]["findings"] < min_findings for name in ARMS)},
            "repos": per_repo}
