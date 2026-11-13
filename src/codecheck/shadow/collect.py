"""Match PR threads back to the findings we posted, and capture human feedback on them."""

from collections import Counter

from codecheck.core.render import FINDING_MARKER
from codecheck.models import PRRef
from codecheck.shadow.store import ShadowStore

REPLY_MARKER = "code-check:reply"


def _is_bot(author: str) -> bool:
    return author.endswith("[bot]")


def _own_marker(body: str) -> str | None:
    """Finding id from the trailing marker we render; a marker earlier in the (unescaped) message is ignored."""
    matches = list(FINDING_MARKER.finditer(body))
    if not matches or body[matches[-1].end():].strip():
        return None
    return matches[-1].group(1)


def collect_outcomes(provider, shadow: ShadowStore, ref: PRRef, me: str) -> dict:
    run_ids = shadow.runs_for_pr(str(ref))
    if not run_ids:
        return {"runs": 0, "outcomes": 0, "human_comments": 0}
    findings: dict[str, tuple[str, dict]] = {}
    for rid in run_ids:
        for rec in shadow.read(rid):
            if rec.get("type") == "finding" and isinstance(rec.get("id"), str):
                findings[rec["id"]] = (rid, rec)
    latest_run = run_ids[-1]
    outcomes = humans = 0
    for thread in provider.list_threads(ref):
        if not thread.comments:
            continue
        root = thread.comments[0]
        fid = _own_marker(root.body) if root.author == me else None
        if fid is not None and fid in findings and findings[fid][1].get("file") == thread.path:
            rid = findings[fid][0]
            replies = [
                {"author": c.author, "body": c.body}
                for c in thread.comments[1:]
                if c.author != me and not _is_bot(c.author) and REPLY_MARKER not in c.body
            ]
            shadow.append(rid, {
                "type": "outcome", "finding_id": fid, "thread_id": thread.id,
                "resolved": thread.is_resolved, "outdated": thread.is_outdated,
                "replies": replies, "reactions": dict(Counter(root.reactions)),
            })
            outcomes += 1
        elif root.author != me and not _is_bot(root.author):
            near = next(
                (f_id for f_id, (_, f) in findings.items()
                 if f.get("file") == thread.path and thread.line is not None
                 and isinstance(f.get("line"), int) and abs(f["line"] - thread.line) <= 3),
                None,
            )
            shadow.append(latest_run, {
                "type": "human_comment", "thread_id": thread.id, "path": thread.path, "line": thread.line,
                "author": root.author, "body": root.body, "matches_finding": near,
            })
            humans += 1
    return {"runs": len(run_ids), "outcomes": outcomes, "human_comments": humans}
