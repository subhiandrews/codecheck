"""Match PR threads back to the findings we posted, and capture human feedback on them."""

from collections import Counter

from codecheck.core.render import FINDING_MARKER
from codecheck.models import PRRef, is_bot_login
from codecheck.shadow.roles import RoleResolver
from codecheck.shadow.store import ShadowStore

REPLY_MARKER = "code-check:reply"


def _is_bot(comment) -> bool:
    return comment.is_bot or is_bot_login(comment.author)


def _withdraw_bot_feedback(shadow: ShadowStore, run_ids: list[str], thread_id: str, author: str) -> int:
    """A thread recorded as human feedback by an earlier version whose author is now recognised as automated.

    The newest record per thread wins everywhere feedback is read, so a `withdrawn` record supersedes the old one."""
    newest = None
    for rid in run_ids:
        for rec in shadow.read(rid):
            is_newer = newest is None or (rec.get("ts") or 0) >= (newest[1].get("ts") or 0)
            if rec.get("type") == "human_comment" and rec.get("thread_id") == thread_id and is_newer:
                newest = (rid, rec)
    if newest is None or newest[1].get("withdrawn"):
        return 0
    rid, old = newest
    shadow.append(rid, {"type": "human_comment", "thread_id": thread_id, "path": old.get("path"), "line": old.get("line"),
                        "author": author, "body": "", "matches_finding": None, "withdrawn": True})
    return 1


def _own_marker(body: str) -> str | None:
    """Finding id from the trailing marker we render; a marker earlier in the (unescaped) message is ignored."""
    matches = list(FINDING_MARKER.finditer(body))
    if not matches or body[matches[-1].end():].strip():
        return None
    return matches[-1].group(1)


def _pr_author(provider, ref: PRRef, shadow: ShadowStore, run_ids: list[str]) -> str | None:
    """The PR author, from a recorded run when it has one, else from the host. None when neither says."""
    for rid in reversed(run_ids):
        for rec in shadow.read(rid):
            if rec.get("type") == "run" and isinstance(rec.get("pr_author"), str) and rec["pr_author"]:
                return rec["pr_author"]
    try:
        author = provider.get_pr(ref).author
    except Exception:  # noqa: BLE001 - collection must not fail over a missing role
        return None
    return author if isinstance(author, str) and author else None


def collect_outcomes(provider, shadow: ShadowStore, ref: PRRef, me: str) -> dict:
    run_ids = shadow.runs_for_pr(str(ref))
    if not run_ids:
        return {"runs": 0, "outcomes": 0, "human_comments": 0}
    findings: dict[str, tuple[str, dict]] = {}  # newest run wins (run_ids is chronological)
    by_run: dict[str, list[tuple[str, dict]]] = {rid: [] for rid in run_ids}
    for rid in run_ids:
        for rec in shadow.read(rid):
            if rec.get("type") == "finding" and isinstance(rec.get("id"), str):
                findings[rec["id"]] = (rid, rec)
                by_run[rid].append((rid, rec))
    latest_run = run_ids[-1]  # runs_for_pr is chronological
    outcomes = humans = 0
    roles = RoleResolver(provider, ref.repo, me, _pr_author(provider, ref, shadow, run_ids))  # one lookup per login
    for thread in provider.list_threads(ref):
        if not thread.comments:
            continue
        root = thread.comments[0]
        fid = _own_marker(root.body) if root.author == me else None
        if fid is not None and fid in findings and findings[fid][1].get("file") == thread.path:
            rid = findings[fid][0]
            replies = [
                {"author": c.author, "body": c.body, "role": roles.role(c.author, c.association)}
                for c in thread.comments[1:]
                if c.author != me and not _is_bot(c) and REPLY_MARKER not in c.body
            ]
            shadow.append(rid, {
                "type": "outcome", "finding_id": fid, "thread_id": thread.id,
                "resolved": thread.is_resolved, "outdated": thread.is_outdated,
                "replies": replies, "reactions": dict(Counter(root.reactions)),
                "resolved_by_role": roles.role(thread.resolved_by) if thread.resolved_by else None,
            })
            outcomes += 1
        elif root.author != me and _is_bot(root):
            _withdraw_bot_feedback(shadow, run_ids, thread.id, root.author)
        elif root.author != me:
            near, home = None, latest_run
            for rid in reversed(run_ids):  # newest run whose own findings it matches
                near = next(
                    (f.get("id") for _, f in by_run[rid]
                     if f.get("file") == thread.path and thread.line is not None
                     and isinstance(f.get("line"), int) and abs(f["line"] - thread.line) <= 3),
                    None,
                )
                if near is not None:
                    home = rid
                    break
            shadow.append(home, {
                "type": "human_comment", "thread_id": thread.id, "path": thread.path, "line": thread.line,
                "author": root.author, "body": root.body, "matches_finding": near,
                "role": roles.role(root.author, root.association),
            })
            humans += 1
    return {"runs": len(run_ids), "outcomes": outcomes, "human_comments": humans}
