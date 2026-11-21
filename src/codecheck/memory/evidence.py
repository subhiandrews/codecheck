"""Evidence out of the shadow store. Shared by `memory measure` (read-only) and `memory consolidate` (writes the store).

Read-only on the shadow store. Runs are taken oldest first (`ordered_run_ids`, never `run_ids`). The role of whoever gave
a signal is read from the record when it has one and is `unknown` for records written before roles existed. Text kept
locally (a quote and the finding's message) is scrubbed and capped here, before it can reach any store.
"""

from collections import Counter
from dataclasses import dataclass, field

from codecheck.core.panel import detect_language
from codecheck.memory.categories import ANY, canonical_category
from codecheck.memory.model import (
    DEFAULT_HOST,
    UNKNOWN,
    Evidence,
    Scope,
    clean_role,
    evidence_id,
    lesson_id,
)
from codecheck.shadow.export import finding_label, run_feedback
from codecheck.shadow.scrub import scrub_text
from codecheck.shadow.store import ShadowStore

QUOTE_CAP = 240
MESSAGE_CAP = 400

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


def clip(value: object, cap: int) -> str | None:
    """Scrubbed, then cut to `cap` characters. Anything that is not text, or is empty, is None."""
    if not isinstance(value, str) or not value.strip():
        return None
    return scrub_text(value.strip())[:cap]


def path_glob(path: object) -> str:
    """The first two directories of a path plus `/**`; a file at the top level is `*`. Never absolute, never `..`."""
    if not isinstance(path, str):
        return "*"
    dirs = [p for p in path.replace("\\", "/").split("/")[:-1] if p not in ("", ".", "..")]
    return "/".join(dirs[:2]) + "/**" if dirs else "*"


def scope_for(repo: str, path: object, category: str, host: str = DEFAULT_HOST) -> Scope:
    language = detect_language(path) if isinstance(path, str) else None
    return Scope(host, repo, path_glob(path), language or "", category)


def _evidence(scope: Scope, pr: str, thread_id: str, signal: str, ts: float, version: object, *, role: object = None,
              quote: object = None, finding: dict | None = None) -> Evidence:
    kind = "miss" if signal == "human_miss" else "finding"
    return Evidence(
        id=evidence_id(scope.host, scope.repo, pr, thread_id, signal), lesson_id=lesson_id(scope, kind), scope=scope,
        signal=signal, ts=ts, pr=pr, thread=thread_id, role=clean_role(role),
        tool_version=version if isinstance(version, str) else None, quote=clip(quote, QUOTE_CAP), finding=finding,
    )


def outcome_signal(outcome: dict) -> str | None:
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


def _signal_role(signal: str, outcome: dict) -> str:
    """Who gave the signal. A verdict records no role yet, so it is `unknown`; a resolve has the resolver's role."""
    if signal.startswith("resolved"):
        return clean_role(outcome.get("resolved_by_role"))
    if signal == "reply":
        replies = [r for r in outcome.get("replies") or [] if isinstance(r, dict)]
        return clean_role(replies[-1].get("role")) if replies else UNKNOWN
    return UNKNOWN


def _last_reply_text(outcome: dict) -> object:
    replies = [r for r in outcome.get("replies") or [] if isinstance(r, dict)]
    return replies[-1].get("body") if replies else None


def load_runs(shadow: ShadowStore) -> list[RunData]:
    """Every run that recorded a repo and PR, oldest first."""
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
            about = {"severity": f.get("severity") if isinstance(f.get("severity"), str) else None,
                     "message": clip(f.get("message"), MESSAGE_CAP)}
            outcome = outcomes.get(f["id"])
            signal = outcome_signal(outcome) if outcome else None
            if outcome and signal:
                data.evidence.append(_evidence(
                    scope, data.pr, str(outcome.get("thread_id")), signal, data.ts, version,
                    role=_signal_role(signal, outcome), quote=_last_reply_text(outcome), finding=about))
                data.counts[f"signal:{signal}"] += 1
            for h in (h for h in humans.values() if h.get("matches_finding") == f["id"]):
                data.evidence.append(_evidence(scope, data.pr, str(h.get("thread_id")), "human_matched", data.ts, version,
                                               role=h.get("role"), quote=h.get("body"), finding=about))
                data.counts["signal:human_matched"] += 1
            label = finding_label(f["id"], outcomes, humans)
            if label in ("confirmed", "disputed"):
                data.labelled.append((scope, label))
        for h in humans.values():
            data.counts["human_comments"] += 1
            if not h.get("matches_finding"):
                scope = scope_for(data.repo, h.get("path"), ANY)
                data.misses.append(scope)
                data.evidence.append(_evidence(scope, data.pr, str(h.get("thread_id")), "human_miss", data.ts, version,
                                               role=h.get("role"), quote=h.get("body")))
                data.counts["signal:human_miss"] += 1
        runs.append(data)
    return runs
