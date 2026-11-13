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


def export_golden(shadow: ShadowStore, out_path: Path) -> int:
    rows = []
    for rid in shadow.run_ids():
        recs = shadow.read(rid)
        run = next((r for r in recs if r.get("type") == "run"), None)
        outcomes = {r["finding_id"]: r for r in recs if r.get("type") == "outcome" and "finding_id" in r}
        humans = {r["thread_id"]: r for r in recs if r.get("type") == "human_comment" and "thread_id" in r}
        diff = shadow.read_diff(rid)
        if run is None or not diff or not (outcomes or humans):
            continue
        matched = {h.get("matches_finding") for h in humans.values() if h.get("matches_finding")}
        findings = []
        for r in (x for x in recs if x.get("type") == "finding"):
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
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data.encode("utf-8"))
    finally:
        os.close(fd)
    return len(rows)
