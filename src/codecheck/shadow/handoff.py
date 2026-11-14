"""Pure pieces of the hand-off: compare human comments with our findings, and narrate a run.

`handoff.json` (SCHEMA 1) holds, from the design's section 3: the run (`run_id`, `skill`, `mode`, `repo`, `pr`,
`pr_url`, `head_sha`, `tool_version`, `config_hash`, `models`, `panel`, `blind`), each reviewer's ingest status
(`reviewers`), the aggregated `findings` (ids and order as `post` gives them), the aggregator's `dropped` list, the
`decision`, transcript `signals` (tool calls and errors, retries, permission denials, turns and output tokens per
transcript file), `ignored_guidance` lines from the transcripts, the human `comparison` (or `comparison_reason`), and
`transcript`, `transcript_reason`, `transcript_files`.

Deliberately absent: per-reviewer tokens and duration (subagent transcripts are named by file, not by panel member,
so they cannot be attributed reliably; the per-file counts in `signals` are what is known) and stale-PR retries (the
skill restarts from `precheck` with a new run id, so one run cannot see them).
"""

import re
from collections.abc import Sequence

from codecheck.models import IssueComment, Review

SCHEMA = 1
# Only an explicit leading severity word counts; anything else is left for a person to label.
_SEVERITY = re.compile(r"\s*[*_]*\s*(critical|warning|suggestion)(?![a-z])", re.IGNORECASE)


def implied_severity(body: str) -> str | None:
    m = _SEVERITY.match(body or "")
    return m.group(1).lower() if m else None


def build_general_comments(reviews: Sequence[Review], issue_comments: Sequence[IssueComment], *, pr_author: str) -> list[dict]:
    """Review bodies and PR-level comments from people. The caller scrubs the bodies."""
    out = []
    for r in reviews:
        if r.is_bot or r.author == pr_author or _skip_body(r.body):
            continue
        out.append({"kind": "review", "author": r.author, "state": r.state, "body": r.body[:300], "created_at": r.submitted_at})
    for c in issue_comments:
        if c.is_bot or _skip_body(c.body):
            continue
        out.append({"kind": "comment", "author": c.author, "state": None, "body": c.body[:300], "created_at": c.created_at})
    return out


def _skip_body(body: str) -> bool:
    return not (body or "").strip() or "<!-- code-check:run:" in body


def build_comparison(records: Sequence[dict], findings: Sequence[dict], general_comments: Sequence[dict] = ()) -> dict:
    """`records` are a run's shadow records; the newest `human_comment` per thread wins, as in export."""
    latest: dict[str, dict] = {}
    for rec in records:
        if rec.get("type") == "human_comment" and isinstance(rec.get("thread_id"), str):
            latest[rec["thread_id"]] = rec
    severity: dict[str, str | None] = {}
    for f in findings:
        if isinstance(f.get("id"), str):
            severity.setdefault(f["id"], f.get("severity"))
    comments, matched, misses = [], set(), []
    for rec in latest.values():
        fid = rec.get("matches_finding")
        human = implied_severity(rec.get("body") or "")
        ours = severity.get(fid) if fid else None
        comments.append({
            "thread_id": rec["thread_id"], "path": rec.get("path"), "line": rec.get("line"), "author": rec.get("author"),
            "matched_finding": fid, "human_severity": human, "finding_severity": ours,
            "severity_agrees": (human == ours) if human and ours else None,
        })
        if fid:
            matched.add(fid)
        else:
            misses.append(rec["thread_id"])
    return {"human_comments": comments, "unraised_findings": [fid for fid in severity if fid not in matched],
            "misses": misses, "general_comments": list(general_comments)}


def render_markdown(h: dict) -> str:
    where = h.get("pr_url") or h.get("pr") or "local branch"
    lines = [
        f"# Hand-off for run {h['run_id']}", "",
        f"- Skill: {h.get('skill')} ({h.get('mode')} mode) on {h.get('repo')}, {where}",
        f"- Head: {h.get('head_sha')}" + (" (blind: reviewers did not see existing comments)" if h.get("blind") else ""),
    ]
    decision = h.get("decision")
    lines.append(f"- Decision: {decision['event']} ({decision.get('reason')})" if decision else "- Decision: none recorded")
    lines += ["", "## Reviewers", ""]
    for name, r in (h.get("reviewers") or {}).items():
        state = f"{r.get('valid')} valid findings" if r.get("output") else "no output"
        lines.append(f"- {name}: {state}" + (f"; errors: {'; '.join(r['errors'])}" if r.get("errors") else ""))
    lines += ["", "## Findings", ""]
    lines += [f"- {f['severity']} {f['category']} at {f['file']}:{f['line']} ({f['id']})" for f in h.get("findings") or []]
    if not h.get("findings"):
        lines.append("- none")
    lines += ["", "## Human comments", ""]
    cmp = h.get("comparison")
    if cmp is None:
        lines.append(f"- No comparison: {h.get('comparison_reason')}")
    else:
        for c in cmp["human_comments"]:
            match = f"matched {c['matched_finding']}" if c["matched_finding"] else "missed by us"
            lines.append(f"- {c['thread_id']} by {c['author']} at {c['path']}:{c['line']}: {match}"
                         + (f", human says {c['human_severity']}, we said {c['finding_severity']}" if c["human_severity"] else ""))
        for g in cmp.get("general_comments") or []:
            state = f" ({g['state']})" if g.get("state") else ""
            lines.append(f"- {g['kind']} by {g['author']}{state}: {(g.get('body') or '')[:120]}")
        lines.append(f"- Findings no human raised: {', '.join(cmp['unraised_findings']) or 'none'}")
    lines += ["", "## Session", ""]
    signals = h.get("signals")
    if not h.get("transcript"):
        lines.append(f"- No transcript: {h.get('transcript_reason')}")
    elif signals:
        lines.append("- Signals cover this run only (subagents by RUN_DIR, main session by time window)")
        lines.append(f"- {signals['tool_calls']} tool calls, {len(signals['tool_errors'])} failed, "
                     f"{signals['retries']} retries, {signals['permission_denials']} permission denials")
        lines += [f"- {a['name']}: {a['turns']} turns, {a['output_tokens']} output tokens" for a in signals["agents"]]
    lines += [f"- Ignored guidance: {g}" for g in h.get("ignored_guidance") or []]
    return "\n".join(lines) + "\n"
