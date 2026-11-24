"""Match PR threads back to the findings we posted, and capture human feedback on them."""

from collections import Counter

from codecheck.core.render import FINDING_MARKER, parse_comment
from codecheck.models import PRRef, comment_is_ours, is_bot_login, same_login
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
    for thread in provider.list_threads(ref):
        if not thread.comments:
            continue
        root = thread.comments[0]
        fid = _own_marker(root.body) if comment_is_ours(root, me) else None
        if fid is not None and fid in findings and findings[fid][1].get("file") == thread.path:
            rid = findings[fid][0]
            replies = [
                {"author": c.author, "body": c.body}
                for c in thread.comments[1:]
                if not comment_is_ours(c, me) and not _is_bot(c) and REPLY_MARKER not in c.body
            ]
            shadow.append(rid, {
                "type": "outcome", "finding_id": fid, "thread_id": thread.id,
                "resolved": thread.is_resolved, "outdated": thread.is_outdated,
                "replies": replies, "reactions": dict(Counter(root.reactions)),
            })
            outcomes += 1
        elif not comment_is_ours(root, me) and _is_bot(root):
            _withdraw_bot_feedback(shadow, run_ids, thread.id, root.author)
        elif not comment_is_ours(root, me):
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
            })
            humans += 1
    return {"runs": len(run_ids), "outcomes": outcomes, "human_comments": humans}


def record_thread_findings(shadow: ShadowStore, run_id: str, pr: str, threads, me: str, ci_login: str = "") -> int:
    """Record the findings of a code-check review that the PR's threads show, as `finding` and `outcome` records.

    The review may have run where this store is not (CI), so the threads are the only copy of its findings here.
    Only a root comment that ends with our finding marker and comes from us or from `ci_login` counts. A finding the
    store already holds for this PR keeps its run: only the outcome is appended there, so no run is copied. A verdict recorded for the
    thread later turns the outcome into evidence."""
    home: dict[str, str] = {}  # finding id -> the newest run of this PR that already holds the finding
    for rid in shadow.runs_for_pr(pr):
        for r in shadow.read(rid):
            if r.get("type") == "finding" and isinstance(r.get("id"), str):
                home[r["id"]] = rid
    n = 0
    for t in threads:
        root = t.comments[0] if t.comments else None
        if root is None or not (comment_is_ours(root, me) or (ci_login and same_login(root.author, ci_login, root.is_bot))):
            continue
        fid = _own_marker(root.body)
        parsed = parse_comment(root.body) if fid else None
        if parsed is None or not t.path or t.line is None:
            continue
        where = home.get(fid)  # like `collect_outcomes`, the outcome goes to the run that holds the finding (and the diff)
        if where is None:
            shadow.append(run_id, {"type": "finding", "id": fid, "file": t.path, "line": t.line, "source": "thread", **parsed})
            where = home[fid] = run_id
        shadow.append(where, {
            "type": "outcome", "finding_id": fid, "thread_id": t.id, "resolved": t.is_resolved, "outdated": t.is_outdated,
            "replies": [{"author": c.author, "body": c.body} for c in t.comments[1:]
                        if not comment_is_ours(c, me) and not _is_bot(c) and REPLY_MARKER not in c.body],
            "reactions": dict(Counter(root.reactions)),
        })
        n += 1
    return n
