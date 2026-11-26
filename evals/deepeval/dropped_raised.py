"""Which findings the aggregator dropped were later raised by a person? Evidence only: no model, no tokens, nothing changes.

Reads the hand-offs in the shadow store. For each human comment that no kept finding matched by location, it takes the
comment's `related` findings (always recomputed with `related_findings`, uncapped, since the stored list keeps only 3) and keeps the
dropped ones. The result is a ledger by drop reason: how many drops there were, and which of them a person's comment
looks like (a pointer to read, not a verdict; a human raising something does not make the drop wrong).

Usage:
  python -B evals/deepeval/dropped_raised.py [--store DIR] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

from codecheck.shadow.handoff import related_findings

STORE = Path(os.environ.get("CODE_CHECK_SHADOW_STORE", "~/.local/share/code-check/shadow")).expanduser()


def _handoffs(store: Path) -> list[dict]:
    out = []
    for path in sorted(store.glob("*.handoff/handoff.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("comparison"), dict):
            out.append(data)
    return out


def _dicts(items) -> list[dict]:
    """The hand-off keeps the aggregator's lists as the agent wrote them, so skip anything that is not an object."""
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _reason(item: dict) -> str:
    """The drop reason as text: the aggregator's list is stored as written, so it may not be a string."""
    reason = item.get("reason")
    return reason if isinstance(reason, str) and reason else "unknown"


def _reviewers(item: dict) -> list[str]:
    """The reviewers who raised a dropped finding; the aggregator's list is stored as written, so keep only text."""
    names = item.get("reviewers")
    return [n for n in names if isinstance(n, str)] if isinstance(names, list) else []


def _dropped_hits(handoff: dict) -> list[dict]:
    """One row per (human comment, dropped finding) pair the comment looks related to."""
    dropped = _dicts(handoff.get("dropped"))
    rows = []
    for c in handoff["comparison"].get("human_comments") or []:
        if c.get("matched_finding"):
            continue
        for d in dropped:  # each drop on its own: the stored `related` keeps only 3, and carries no reviewers
            for r in related_findings(c.get("body") or "", c.get("path"), [], [d], limit=None):
                rows.append({"run_id": handoff.get("run_id"), "pr": handoff.get("pr"), "reason": _reason(r),
                             "file": r.get("file"), "line": r.get("line"), "reviewers": _reviewers(d),
                             "comment_author": c.get("author"), "comment_line": c.get("line"), "shared": r.get("shared")})
    return rows


def ledger(handoffs: list[dict]) -> dict:
    drops, hits, distinct = Counter(), [], set()
    for h in handoffs:
        drops.update(_reason(d) for d in _dicts(h.get("dropped")))
        for row in _dropped_hits(h):
            hits.append(row)
            distinct.add(tuple(str(row[k]) for k in ("run_id", "file", "line", "reason")))  # one drop, however many comments
    raised = Counter(reason for _, _, _, reason in distinct)
    return {"runs": len(handoffs), "dropped_by_reason": dict(drops), "dropped_with_related_comment": dict(raised), "hits": hits}


def summary(result: dict) -> list[str]:
    lines = [f"{result['runs']} hand-offs; reason: dropped / with a related human comment"]
    for reason, n in sorted(result["dropped_by_reason"].items()):
        lines.append(f"  {reason}: {n} / {result['dropped_with_related_comment'].get(reason, 0)}")
    lines += [f"- {h['pr']} {h['file']}:{h['line']} dropped ({h['reason']}) by {','.join(h['reviewers']) or '?'}; "
              f"{h['comment_author']} commented at line {h['comment_line']}; shared: {', '.join(h['shared'] or [])}"
              for h in result["hits"]]
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--store", type=Path, default=STORE)
    ap.add_argument("--json", type=Path, dest="out")
    args = ap.parse_args(argv)
    result = ledger(_handoffs(args.store.expanduser()))
    if args.out:
        args.out.expanduser().write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print("\n".join(summary(result)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
