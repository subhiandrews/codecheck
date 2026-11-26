"""What happened to the findings we posted? Read-only, no model calls, no tokens.

For each posted review in a `posted-reviews-*.json` file it asks GitHub (`gh api`, GET only) for the replies to our
inline comments, reactions, later reviews and comments by other people, the PR state, and the diff between the head
we reviewed and the head now. Each inline finding gets one signal:

  candidate_acted_on    someone replied in the thread AND the code changed within NEAR lines of the cited line
  changed_without_reply the code changed near the cited line and nobody said anything
  replied_no_change     someone replied but the cited area is untouched: a claim, not a fix
  no_response_yet       nothing yet; this is not evidence the finding was wrong

A signal is a candidate only: an author's reply is not evidence that the code changed, so a person reads the diff
before any label is written. A mention of us is reported once per PR (`mentions_us`), never counted as a reply.
`replied_unverified` / `unverified` mean the cited line or the file's patch was unavailable.

Usage:
  python -B evals/deepeval/outcomes.py --posted ~/.local/share/code-check-evals/posted-reviews-2026-11-24.json \
      [--out outcomes.json] [--near 10]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

NEAR = 10
COMPARE_CAP = 300  # GitHub lists at most this many changed files for a whole comparison, even when paged
REPO_RE = re.compile(r"^[A-Za-z0-9_][\w.-]*/[A-Za-z0-9_][\w.-]*$")  # never starts with "-": it reaches gh's argv
KIND_RE = re.compile(r"^\*\*(\w+)\*\*")
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")

Gh = Callable[[str], object]


def gh_api(endpoint: str) -> object:
    """GET one endpoint with `gh api` and return the parsed JSON; paginated arrays are joined."""
    if not endpoint.startswith(("repos/", "user")):
        raise ValueError(f"unexpected endpoint {endpoint!r}")
    # argv list; the endpoint is built from validated repo and integer parts
    out = subprocess.run(  # noqa: S603
        ["gh", "api", "--paginate", endpoint], capture_output=True, text=True, check=True, timeout=120  # noqa: S607
    ).stdout
    decoder, pos, parts = json.JSONDecoder(), 0, []
    while pos < len(out):
        while pos < len(out) and out[pos].isspace():
            pos += 1
        if pos >= len(out):
            break
        value, pos = decoder.raw_decode(out, pos)
        parts.append(value)
    if parts and all(isinstance(p, list) for p in parts):
        return [item for p in parts for item in p]
    if parts and all(isinstance(p, dict) for p in parts):  # a paged compare: one object per page
        merged = dict(parts[0])
        seen = {f.get("filename") for f in merged.get("files", [])}
        for page in parts[1:]:
            merged["files"] = merged.get("files", []) + [f for f in page.get("files", []) if f.get("filename") not in seen]
            seen.update(f.get("filename") for f in page.get("files", []))
        return merged
    return parts[0] if len(parts) == 1 else parts


def touched_old_lines(patch: str) -> set[int]:
    """Old-side line numbers a compare patch removes, plus the old line each insertion sits next to."""
    touched: set[int] = set()
    old = 0
    for raw in patch.splitlines():
        hunk = HUNK_RE.match(raw)
        if hunk:
            old = int(hunk.group(1))
            continue
        if raw.startswith("-"):
            touched.add(old)
            old += 1
        elif raw.startswith("+"):
            touched.add(old)
        elif raw.startswith("\\"):
            continue
        else:
            old += 1
    return touched


def code_change(files: list[dict], path: str, line: int, near: int, capped: bool = False) -> str:
    """changed_near, file_changed (elsewhere in the file), file_untouched, or unknown.

    `line` is the cited line in the reviewed commit, the old side of the compare. Unknown means no line, no patch to
    read, or (when `capped`) a file the cap may have cut off, so its absence does not show it is untouched."""
    for f in files:
        if path in (f.get("filename"), f.get("previous_filename")):  # a rename lists the old path here
            if not line or not f.get("patch"):
                return "unknown"
            if any(abs(t - line) <= near for t in touched_old_lines(f["patch"])):
                return "changed_near"
            return "file_changed"
    return "unknown" if capped else "file_untouched"


def signal(replied: bool, change: str) -> str:
    if change == "changed_near":
        return "candidate_acted_on" if replied else "changed_without_reply"
    if change == "unknown":
        return "replied_unverified" if replied else "unverified"
    return "replied_no_change" if replied else "no_response_yet"


def _human(user: dict | None, me: str) -> bool:
    return bool(user) and user.get("login") != me and user.get("type") != "Bot"


def collect(entry: dict, gh: Gh, me: str, near: int = NEAR) -> dict:
    repo, pr, rid = entry["repo"], int(entry["pr"]), int(entry["review_id"])
    if not REPO_RE.match(repo):
        raise ValueError(f"bad repo {repo!r}")
    base = f"repos/{repo}"
    review = gh(f"{base}/pulls/{pr}/reviews/{rid}")
    posted_at, reviewed = review["submitted_at"], review["commit_id"]
    pull = gh(f"{base}/pulls/{pr}")
    head = pull["head"]["sha"]
    ours = gh(f"{base}/pulls/{pr}/reviews/{rid}/comments")
    everything = gh(f"{base}/pulls/{pr}/comments")
    issue = gh(f"{base}/issues/{pr}/comments")
    reviews = gh(f"{base}/pulls/{pr}/reviews")
    files = [] if head == reviewed else gh(f"{base}/compare/{reviewed}...{head}").get("files", [])
    mentioned = any(_human(c.get("user"), me) and c["created_at"] > posted_at and f"@{me}" in (c.get("body") or "")
                    for c in issue)
    findings = []
    by_id = {c["id"]: c for c in everything}
    sizes = {f["filename"]: f"+{f.get('additions', 0)}/-{f.get('deletions', 0)}" for f in files}
    for c in ours:
        # the review-scoped listing returns null lines for outdated comments; the PR-wide listing keeps them
        full = {**c, **{k: v for k, v in by_id.get(c["id"], {}).items() if v is not None}}
        # original_line is the line in the reviewed commit, which the compare's old side numbers. `line` is the
        # current head's and shifts when a push adds or removes lines above the comment, so it is never a fallback:
        # without original_line the code change is unknown
        line = full.get("original_line") or 0
        replies = [r for r in everything if r.get("in_reply_to_id") == c["id"] and _human(r.get("user"), me)]
        change = code_change(files, c["path"], line, near, capped=len(files) >= COMPARE_CAP)
        severity = KIND_RE.match(c.get("body") or "")
        findings.append({
            "comment_id": c["id"], "path": c["path"], "line": line,
            "kind": severity.group(1) if severity else "",
            "replies": len(replies), "reactions": (c.get("reactions") or {}).get("total_count", 0),
            "code": change, "file_delta": sizes.get(c["path"], "+0/-0"),
            "signal": signal(bool(replies), change),
        })
    return {
        "repo": repo, "pr": pr, "review_id": rid, "reviewed_head": reviewed, "head": head,
        "state": pull["state"], "merged": bool(pull.get("merged_at")),
        "new_reviews_by_others": sum(1 for r in reviews if _human(r.get("user"), me) and r["submitted_at"] > posted_at),
        "comments_by_others": sum(1 for c in issue if _human(c.get("user"), me) and c["created_at"] > posted_at),
        "mentions_us": mentioned, "findings": findings,
    }


def _detail(exc: Exception) -> str:
    """gh's own message when there is one (auth, 404, rate limit); `str(CalledProcessError)` has only the exit status."""
    stderr = getattr(exc, "stderr", None)
    return (stderr.strip() if isinstance(stderr, str) else "") or str(exc)


def run(posted: dict, gh: Gh, near: int = NEAR) -> dict:
    me = gh("user")["login"]
    rows = []
    for e in posted["prs"]:
        if e.get("status") != "posted" or not e.get("review_id"):
            continue
        try:
            rows.append(collect(e, gh, me, near))
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, KeyError, TypeError, ValueError, OSError) as exc:
            rows.append({"repo": e.get("repo"), "pr": e.get("pr"), "error": f"{type(exc).__name__}: {_detail(exc)}"[:300],
                         "findings": []})  # one unreadable PR must not lose the others
    return {"source": posted.get("posted_on"), "near": near, "prs": rows,
            "note": "signals are candidates; read the diff at the head before writing any label"}


def summary(result: dict) -> list[str]:
    lines = []
    for p in result["prs"]:
        if p.get("error"):
            lines.append(f"{p['repo']}#{p['pr']} ERROR {p['error']}")
        for f in p["findings"]:
            lines.append(f"{p['repo']}#{p['pr']} {f['path']}:{f['line']} {f['kind'] or '-'}: {f['signal']} "
                         f"(replies {f['replies']}, reactions {f['reactions']}, code {f['code']} {f['file_delta']}, "
                         f"pr {p['state']})")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--posted", required=True, type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--near", type=int, default=NEAR)
    args = ap.parse_args(argv)
    try:
        result = run(json.loads(args.posted.expanduser().read_text(encoding="utf-8")), gh_api, args.near)
        if args.out:
            args.out.expanduser().write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, ValueError, KeyError, TypeError,
            AttributeError) as exc:  # no gh, bad auth, a malformed posted file, an unwritable --out
        print(f"outcomes: {type(exc).__name__}: {_detail(exc)}"[:300], file=sys.stderr)
        return 1
    print("\n".join(summary(result)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
