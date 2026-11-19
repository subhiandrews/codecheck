"""Send hand-offs to an OTLP collector. Local files stay the source of truth: this only reads them.

A run's last send is remembered in `<run_id>.handoff/telemetry.json` (the payload's digest, so an unchanged run is not
sent again, plus the last error). That file is the outbox: a failed send leaves the digest stale, and the next push
(`telemetry push --all`, or the automatic push after `handoff` and `shadow collect`) retries it.
"""

import hashlib
import json
import time
from collections.abc import Iterable
from pathlib import Path

from codecheck.errors import CodeCheckError
from codecheck.runids import check_run_id
from codecheck.shadow.export import finding_label, run_feedback
from codecheck.shadow.handoff import build_comparison, near_finding
from codecheck.shadow.scrub import scrub_text
from codecheck.shadow.store import ShadowStore, write_private
from codecheck.telemetry import otlp
from codecheck.telemetry.build import build_payload, summarize

STATE = "telemetry.json"


def _load(path: Path) -> dict | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _folder(shadow: ShadowStore, run_id: str) -> Path:
    return shadow.root / f"{check_run_id(run_id)}.handoff"


def _ts(rec: dict) -> float:
    ts = rec.get("ts")
    return ts if isinstance(ts, (int, float)) and not isinstance(ts, bool) else 0.0


def feedback_for(shadow: ShadowStore, run_id: str, handoff: dict, rf: dict) -> dict | None:
    """The human feedback on a run as it is now, or None when there is none yet (a local run has none)."""
    pr = handoff.get("pr")
    if not pr:
        return None
    ours = handoff.get("findings") or []
    # As `handoff` compares: every thread's latest record across the PR's runs, matched against this run's findings.
    records = sorted(
        (r for rid in shadow.runs_for_pr(pr) for r in shadow.read(rid) if r.get("type") == "human_comment"), key=_ts)
    rematched = [{**r, "matches_finding": near_finding(ours, r.get("path"), r.get("line"))} for r in records]
    general = ((handoff.get("comparison") or {}).get("general_comments")) or []
    comparison = build_comparison(rematched, ours, general)
    outcomes, humans = rf.get(run_id, ({}, {}))
    labels = {f["id"]: finding_label(f["id"], outcomes, humans) for f in ours if isinstance(f.get("id"), str)}
    if not (comparison["human_comments"] or general or any(v != "unreviewed" for v in labels.values())):
        return None
    seen = [_ts(r) for r in records] + [_ts(r) for r in outcomes.values()]
    return {
        "at": max(seen) if any(seen) else None, "comparison": comparison, "labels": labels,
        "bodies": {r["thread_id"]: " ".join(scrub_text(r.get("body") or "").split())[:300]
                   for r in rematched if isinstance(r.get("thread_id"), str)},
    }


def _rejected(text: str) -> int:
    try:
        data = json.loads(text) if text.strip() else {}
        n = data.get("partialSuccess", {}).get("rejectedSpans", 0) if isinstance(data, dict) else 0
        return int(n) if isinstance(n, (int, str)) and str(n).isdigit() else 0
    except (ValueError, AttributeError):
        return 0


def push_run(ctx, run_id: str, target: otlp.Target | None, *, rf: dict, dry_run: bool = False, force: bool = False) -> dict:
    shadow = ctx.shadow
    run_id = check_run_id(run_id)
    folder = _folder(shadow, run_id)
    handoff = _load(folder / "handoff.json")
    if handoff is None:
        return {"run_id": run_id, "status": "skipped", "reason": "no hand-off for this run"}
    records = shadow.read(run_id)
    ended = (folder / "handoff.json").stat().st_mtime
    started = next((r["ts"] for r in records if r.get("type") == "run" and isinstance(r.get("ts"), (int, float))), ended)
    cfg = ctx.config.telemetry
    resource = target.resource if target is not None else otlp.resource_attributes(ctx.env)
    payload = build_payload(handoff, started=started, ended=ended, resource=resource, content=cfg.content,
                            feedback=feedback_for(shadow, run_id, handoff, rf))
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    result = {"run_id": run_id, **summarize(payload), "bytes": len(body)}
    if dry_run:
        return {**result, "status": "dry-run", "payload": payload}
    state = _load(folder / STATE) or {}
    if state.get("sent") == digest and not force:
        return {**result, "status": "unchanged"}
    attempts = state.get("attempts", 0) if isinstance(state.get("attempts"), int) else 0
    try:
        status, text = otlp.post_json(target.url, target.headers, body, cfg.timeout_seconds)
        error = None if 200 <= status < 300 and not _rejected(text) else f"HTTP {status}: {scrub_text(text)[:200]}".strip()
    except CodeCheckError as e:
        error = scrub_text(str(e))[:300]
    if error is None:
        new = {"sent": digest, "sent_at": time.time(), "attempts": 0, "last_error": None}
    else:
        new = {**state, "attempts": attempts + 1, "last_error": error, "last_attempt_at": time.time()}
    write_private(shadow.handoff_dir(run_id) / STATE, json.dumps(new, indent=1))
    return {**result, "status": "sent", "endpoint": target.host} if error is None else {**result, "status": "failed", "error": error}


def push_runs(ctx, run_ids: Iterable[str], *, dry_run: bool = False, force: bool = False) -> dict:
    """Push runs in order. It stops at the first failed send, so an unreachable collector costs one timeout, not one per run."""
    if ctx.shadow is None:
        return {"telemetry": False, "reason": "shadow mode is disabled (shadow.enabled = false)"}
    cfg = ctx.config.telemetry
    if not cfg.enabled and not dry_run:
        return {"telemetry": False, "reason": "telemetry.enabled is false"}
    target = otlp.resolve_target(cfg, ctx.env)
    if target is None and not dry_run:
        return {"telemetry": False, "reason": "no OTLP endpoint: set telemetry.endpoint or OTEL_EXPORTER_OTLP_ENDPOINT"}
    rf = run_feedback(ctx.shadow)
    results, failed = [], False
    for rid in run_ids:
        if failed:
            results.append({"run_id": rid, "status": "not_attempted"})
            continue
        try:
            res = push_run(ctx, rid, target, rf=rf, dry_run=dry_run, force=force)
        except OSError as e:
            res = {"run_id": rid, "status": "failed", "error": scrub_text(f"{type(e).__name__}: {e}")[:300]}
        failed = res["status"] == "failed"
        results.append(res)
    return {"telemetry": True, "dry_run": dry_run, "endpoint": target.host if target else None, "results": results}


def pushable_runs(shadow: ShadowStore) -> list[str]:
    return [rid for rid in shadow.ordered_run_ids() if (_folder(shadow, rid) / "handoff.json").is_file()]


def auto_push(ctx, run_ids: Iterable[str]) -> dict | None:
    """The automatic push after `handoff` and `shadow collect`. None when telemetry is off, so a run shows no sign of
    it; never raises, because exporting must not change what a command does."""
    if not ctx.config.telemetry.enabled or ctx.shadow is None:
        return None
    try:
        return push_runs(ctx, [r for r in run_ids if (_folder(ctx.shadow, r) / "handoff.json").is_file()])
    except Exception as e:  # noqa: BLE001 - export is best effort by design
        return {"telemetry": False, "error": scrub_text(f"{type(e).__name__}: {e}")[:300]}
