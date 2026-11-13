"""Turn collected outcomes into a golden eval set (auto-labelled, for humans to review)."""

import json
import os
from pathlib import Path

from codecheck.shadow.scrub import scrub
from codecheck.shadow.store import ShadowStore


def _label(outcome: dict | None) -> str:
    if outcome is None:
        return "unreviewed"
    if outcome.get("resolved") or outcome.get("outdated"):
        return "confirmed"
    if outcome.get("replies"):
        return "disputed"
    return "no_signal"


def _latest_by_thread(records: list[tuple[int, dict]]) -> dict[str, tuple[int, dict]]:
    """Newest record per thread across all runs (by record ts, then run order), so a thread counts once."""
    out: dict[str, tuple[int, dict]] = {}
    for order, r in records:
        tid = r.get("thread_id")
        if not isinstance(tid, str):
            continue
        key = (r.get("ts") if isinstance(r.get("ts"), (int, float)) else 0, order)
        if tid not in out or key >= (out[tid][1].get("ts") if isinstance(out[tid][1].get("ts"), (int, float)) else 0, out[tid][0]):
            out[tid] = (order, r)
    return out


def export_golden(shadow: ShadowStore, out_path: Path) -> int:
    runs = [(rid, shadow.read(rid)) for rid in shadow.ordered_run_ids()]
    winners = {}
    for kind in ("outcome", "human_comment"):
        winners[kind] = _latest_by_thread(
            [(i, r) for i, (_, recs) in enumerate(runs) for r in recs if r.get("type") == kind]
        )
    rows = []
    for i, (rid, recs) in enumerate(runs):
        run = next((r for r in recs if r.get("type") == "run"), None)
        # a thread's feedback only counts in the run it was last attributed to
        outcomes = {
            r["finding_id"]: r for r in recs
            if r.get("type") == "outcome" and isinstance(r.get("finding_id"), str)
            and winners["outcome"].get(r.get("thread_id"), (None, None))[1] is r
        }
        humans = {
            r["thread_id"]: r for r in recs
            if r.get("type") == "human_comment" and winners["human_comment"].get(r.get("thread_id"), (None, None))[1] is r
        }
        diff = shadow.read_diff(rid)
        if run is None or not diff or not (outcomes or humans):
            continue
        matched = {h.get("matches_finding") for h in humans.values() if h.get("matches_finding")}
        findings = []
        for r in (x for x in recs if x.get("type") == "finding" and isinstance(x.get("id"), str)):
            label = _label(outcomes.get(r["id"]))
            if r["id"] in matched and label in ("unreviewed", "no_signal"):
                label = "confirmed"
            findings.append({k: r.get(k) for k in ("id", "file", "line", "severity", "category", "message", "reviewers")} | {"label": label})
        rows.append({
            "run_id": rid, "repo": run.get("repo"), "pr": run.get("pr"), "head_sha": run.get("head_sha"),
            "tool_version": run.get("tool_version"), "models": run.get("models"), "diff": diff, "findings": findings,
            "misses": [{k: h.get(k) for k in ("path", "line", "author", "body")} for h in humans.values() if not h.get("matches_finding")],
            "label_status": "auto",
        })
    data = "".join(json.dumps(scrub(r), ensure_ascii=False) + "\n" for r in rows)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if out.is_symlink():
        raise OSError(f"refusing to write through symlink {out}")
    tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data.encode("utf-8"))
        os.replace(tmp, out)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return len(rows)
