"""Pure pieces of the hand-off: compare human comments with our findings, and narrate a run.

`handoff.json` (SCHEMA 1) holds, from the design's section 3: the run (`run_id`, `skill`, `mode`, `repo`, `pr`,
`pr_url`, `head_sha`, `tool_version`, `plugin_root`, `prompts_hash` (12 hex chars over the agent and skill
prompts) and `build_reason` (why they are null), `config_hash`, `models`, `panel`, `blind`), each reviewer's ingest status
(`reviewers`), the aggregated `findings` (ids and order as `post` gives them, each with its scrubbed message capped at 300 characters, and its `rated_because` capped the same when the aggregator gave one), the aggregator's `dropped` list, the
`decision`, transcript `signals` (tool calls and errors, retries, permission denials, turns and output tokens per
transcript file), `ignored_guidance` lines from the transcripts, the human `comparison` (or `comparison_reason`; each human comment carries `in_review`, false when its path was not among the files the reviewers were given, and such a thread is not in `misses`, and `related`, the kept or dropped findings in the same file that share words with an unmatched comment, for a person to check; `general_comments_reason` says why its general comments are missing, null when fine) with `comparison_mode` (`posted`, `unposted` or null), `head_moved`
and `pr_state` (`open`, `closed`, `merged`; both null for local runs, on the posted path, for drafts and when the PR fetch
failed; `head_moved` is also null when the run recorded no head SHA), and
`transcript`, `transcript_reason`, `transcript_files`.

Deliberately absent: per-reviewer tokens and duration (subagent transcripts are named by file, not by panel member,
so they cannot be attributed reliably; the per-file counts in `signals` are what is known) and stale-PR retries (the
skill restarts from `precheck` with a new run id, so one run cannot see them).
"""

import re
from collections.abc import Sequence

from codecheck.core.render import RUN_PREFIX
from codecheck.models import IssueComment, Review

SCHEMA = 1
# Only an explicit leading severity word counts; anything else is left for a person to label.
_SEVERITY = re.compile(r"\s*[*_]*\s*(critical|warning|suggestion)(?![a-z])", re.IGNORECASE)


def implied_severity(body: str) -> str | None:
    m = _SEVERITY.match(body or "")
    return m.group(1).lower() if m else None


# A human comment and a finding can name the same concern from lines far apart (a test file against the code it
# tests, a description against its fix), so `related` pairs them by shared words in the same file. It is a
# pointer for a person to check, never a match: it changes none of `matched_finding`, `misses` or `unraised_findings`.
MIN_SHARED = 3
MAX_RELATED = 3
_WORD = re.compile(r"[a-z_][a-z0-9_]{3,}")
_STOP = frozenset(
    ["about", "above", "after", "again", "also", "because", "been", "before", "being", "between", "both", "bring", "could", "does", "done", "each", "either", "else", "even", "ever", "from", "have", "here", "into", "just", "like", "made", "make", "many", "might", "more", "most", "much", "must", "need", "only", "other", "over", "same", "should", "since", "some", "such", "than", "that", "their", "them", "then", "there", "these", "they", "this", "those", "through", "under", "until", "upon", "very", "want", "were", "what", "when", "where", "which", "while", "will", "with", "would", "your"])


def _terms(text) -> set[str]:
    """Distinctive lowercase words of at least four letters, with a plural or -ed/-ing ending folded away.

    Anything that is not text has no words: the aggregator's dropped list is stored as the agent wrote it."""
    out: set[str] = set()
    if not isinstance(text, str):
        return out
    for word in _WORD.findall(text.lower()):
        if word in _STOP:
            continue
        for suffix in ("ing", "ed", "s"):
            if word.endswith(suffix) and not word.endswith("ss") and len(word) - len(suffix) >= 4:
                word = word[: -len(suffix)]
                break
        out.add(word)
    return out - _STOP


def related_findings(body: str, path, kept: Sequence[dict], dropped: Sequence[dict], *, limit: int | None = MAX_RELATED) -> list[dict]:
    """Kept and dropped findings in the same file that share at least MIN_SHARED words with a human comment.

    Most shared words first, kept before dropped on a tie; at most `limit` (MAX_RELATED by default, None for all)."""
    mine = _terms(body)
    if not isinstance(path, str) or len(mine) < MIN_SHARED:
        return []
    scored = []
    for kind, items in (("kept", kept), ("dropped", dropped)):
        for f in items:
            if not isinstance(f, dict) or f.get("file") != path:
                continue
            shared = sorted(mine & _terms(f.get("message")))
            if len(shared) >= MIN_SHARED:
                scored.append({"kind": kind, "id": f.get("id") if kind == "kept" else None,
                               "reason": f.get("reason") if kind == "dropped" else None,
                               "file": path, "line": f.get("line"), "shared": shared})
    scored.sort(key=lambda r: -len(r["shared"]))  # on the full count; the stored list is cut to 8 words below
    for r in scored:
        r["shared"] = r["shared"][:8]
    return scored if limit is None else scored[:limit]


def build_general_comments(reviews: Sequence[Review], issue_comments: Sequence[IssueComment], *, pr_author: str) -> list[dict]:
    """Review bodies and PR-level comments from people. The caller scrubs the bodies."""
    out = []
    for r in reviews:
        if r.is_bot or r.author == pr_author or _skip_body(r.body):
            continue
        out.append({"kind": "review", "author": r.author, "state": r.state, "body": r.body, "created_at": r.submitted_at})
    for c in issue_comments:
        if c.is_bot or _skip_body(c.body):
            continue
        out.append({"kind": "comment", "author": c.author, "state": None, "body": c.body, "created_at": c.created_at})
    return out


def _skip_body(body: str) -> bool:
    return not (body or "").strip() or RUN_PREFIX in body


def build_comparison(records: Sequence[dict], findings: Sequence[dict], general_comments: Sequence[dict] = (),
                     reviewed: set[str] | None = None, dropped: Sequence[dict] = ()) -> dict:
    """`records` are a run's shadow records; the newest `human_comment` per thread wins, as in export.

    `reviewed` is the set of paths the run's reviewers were given. A thread on any other path is marked
    `in_review: false` and is not a miss, since no reviewer could have seen it; without it no claim is made.
    `dropped` is the aggregator's dropped list; with the kept findings it feeds each unmatched comment's `related`."""
    latest: dict[str, dict] = {}
    for rec in records:
        if rec.get("type") == "human_comment" and isinstance(rec.get("thread_id"), str):
            latest[rec["thread_id"]] = rec
    latest = {tid: rec for tid, rec in latest.items() if not rec.get("withdrawn")}
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
            "severity_agrees": (human == ours) if human and ours else None, "body": " ".join((rec.get("body") or "").split())[:300],
            "related": [] if fid else related_findings(rec.get("body") or "", rec.get("path"), findings, dropped),
        })
        outside = reviewed is not None and isinstance(rec.get("path"), str) and rec["path"] not in reviewed
        if reviewed is not None:
            comments[-1]["in_review"] = not outside
        if fid:
            matched.add(fid)
        elif not outside:
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
    if h.get("prompts_hash"):
        lines.append(f"- Build: {h['prompts_hash']} ({h.get('plugin_root')})")
    else:
        lines.append(f"- Build: unknown ({h.get('build_reason')})")
    models, reasoning = h.get("models") or {}, h.get("reasoning") or {}
    if models:
        lines.append(f"- Runtime: {h.get('runtime') or 'claude'}; models: " + ", ".join(
            f"{name} {model}" + (f" ({reasoning[name]})" if name in reasoning else "") for name, model in models.items()))
    decision = h.get("decision")
    lines.append(f"- Decision: {decision['event']} ({decision.get('reason')})" if decision else "- Decision: none recorded")
    lines += ["", "## Reviewers", ""]
    for name, r in (h.get("reviewers") or {}).items():
        state = f"{r.get('valid')} valid findings" if r.get("output") else "no output"
        lines.append(f"- {name}: {state}" + (f"; errors: {'; '.join(r['errors'])}" if r.get("errors") else ""))
    lines += ["", "## Findings", ""]
    lines += [f"- {f['severity']} {f['category']} at {f['file']}:{f['line']} ({f['id']})"
              + (f" from {', '.join(f['reviewers'])}" if f.get("reviewers") else "")
              + (f": {' '.join(f['message'].split())[:160]}" if (f.get("message") or "").strip() else "")
              for f in h.get("findings") or []]
    if not h.get("findings"):
        lines.append("- none")
    acc = h.get("accounting")
    if acc:
        unaccounted = ", ".join(f"{m} ({n})" for m, n in (acc.get("possibly_unaccounted") or {}).items())
        lines.append(f"- Reviewer findings {acc['reviewer_findings']}: {acc['kept']} kept, {acc['dropped']} dropped"
                     + (f"; possibly unaccounted findings (several from one reviewer may have merged into one item): {unaccounted}"
                        if unaccounted else ""))
    unposted = h.get("comparison_mode") == "unposted" and h.get("comparison") is not None
    lines += ["", "## Human comments (unposted review; compared with the threads as they are now)" if unposted
              else "## Human comments", ""]
    if unposted and h.get("head_moved"):
        lines.append("- Head moved since the review")
    cmp = h.get("comparison")
    if cmp is None:
        lines.append(f"- No comparison: {h.get('comparison_reason')}")
    else:
        for c in cmp["human_comments"]:
            match = f"matched {c['matched_finding']}" if c["matched_finding"] else (
                "not in the reviewed diff" if c.get("in_review") is False else "missed by us")
            if not c["matched_finding"] and (c.get("body") or "").strip():  # a miss is only useful with its text
                match += f": {' '.join(c['body'].split())[:160]}"
            if c.get("related"):
                match += " (possibly the same concern: " + "; ".join(
                    f"kept {r['id']} at {r['file']}:{r['line']}" if r["kind"] == "kept"
                    else f"dropped ({r.get('reason')}) at {r['file']}:{r['line']}" for r in c["related"]) + ")"
            lines.append(f"- {c['thread_id']} by {c['author']} at {c['path']}:{c['line']}: {match}"
                         + (f", human says {c['human_severity']}, we said {c['finding_severity']}" if c["human_severity"] else ""))
        for g in cmp.get("general_comments") or []:
            state = f" ({g['state']})" if g.get("state") else ""
            lines.append(f"- {g['kind']} by {g['author']}{state}: {' '.join((g.get('body') or '').split())[:120]}")
        lines.append(f"- Findings no human raised: {', '.join(cmp['unraised_findings']) or 'none'}")
    lines += ["", "## Session", ""]
    signals = h.get("signals")
    if not h.get("transcript"):
        lines.append(f"- No transcript: {h.get('transcript_reason')}")
    elif signals:
        lines.append(f"- {h['transcript_reason']}" if h.get("transcript_reason")
                     else "- Signals cover this run only (subagents by RUN_DIR, main session by time window)")
        lines.append(f"- {signals['tool_calls']} tool calls, {len(signals['tool_errors'])} failed, "
                     f"{signals['retries']} retries, {signals['permission_denials']} permission denials")
        lines += [f"- {a['name']}: {a['turns']} turns, {a['output_tokens']} output tokens" for a in signals["agents"]]
    lines += [f"- Ignored guidance: {g}" for g in h.get("ignored_guidance") or []]
    return "\n".join(lines) + "\n"
