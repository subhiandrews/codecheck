"""Turn a finished review into shadow-mode records. Recording never changes behavior."""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict

from codecheck import __version__
from codecheck.config import Config
from codecheck.core.findings import finding_id
from codecheck.models import Decision, Finding
from codecheck.shadow.store import ShadowStore


def config_hash(cfg: Config) -> str:
    return hashlib.sha256(json.dumps(asdict(cfg), sort_keys=True, default=str).encode()).hexdigest()[:12]


def record_run(shadow: ShadowStore, meta: Mapping, diff: str, cfg: Config) -> None:
    run_id = meta["run_id"]
    shadow.append(run_id, {
        "type": "run", "skill": meta.get("skill"), "repo": meta.get("repo"), "pr": meta.get("pr"),
        "head_sha": meta.get("head_sha"), "mode": meta.get("mode"), "panel": list(meta["panel"]["members"]),
        "models": dict(meta.get("models", {})), "tool_version": __version__, "config_hash": config_hash(cfg),
        "tokens": None,
    })
    if diff:
        shadow.save_diff(run_id, diff)


def record_post(
    shadow: ShadowStore,
    meta: Mapping,
    *,
    reviewer_findings: Mapping[str, Sequence[Finding]],
    findings: Sequence[Finding],
    inline_ids: set[str],
    decision: Decision,
    incomplete: bool,
    dropped: Sequence[dict],
) -> None:
    run_id = meta["run_id"]
    for reviewer, items in reviewer_findings.items():
        for f in items:
            kept = any(k.file == f.file and abs(k.line - f.line) <= 2 for k in findings)
            shadow.append(run_id, {"type": "reviewer_finding", "reviewer": reviewer, "id": finding_id(f), "kept": kept, **f.to_dict()})
    for f in findings:
        fid = finding_id(f)
        shadow.append(run_id, {"type": "finding", "id": fid, "inline": fid in inline_ids, **f.to_dict()})
    shadow.append(run_id, {"type": "decision", "event": decision.event, "reason": decision.reason, "incomplete": incomplete})
    for d in dropped:
        shadow.append(run_id, {"type": "dropped", "detail": d})
