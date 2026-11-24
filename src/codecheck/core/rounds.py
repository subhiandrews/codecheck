"""Review rounds on one PR: how many of our reviews it has had, and what changed since the last one."""

from collections.abc import Sequence

from codecheck.core.diffmap import parse_diff
from codecheck.core.render import RUN_PREFIX
from codecheck.models import Finding, Review, Severity, same_login


def our_reviews(reviews: Sequence[Review], me: str) -> list[Review]:
    """Our posted code-check reviews; the host is the record, so a review made on another machine counts."""
    return [r for r in reviews if same_login(r.author, me, r.is_bot) and RUN_PREFIX in (r.body or "")]


def count_reviews(reviews: Sequence[Review], me: str) -> int:
    return len(our_reviews(reviews, me))


def last_review_commit(reviews: Sequence[Review], me: str) -> str:
    """The commit our latest code-check review looked at ("" when there is none); other reviews by the same login do not count."""
    mine = our_reviews(reviews, me)
    return max(mine, key=lambda r: r.submitted_at).commit_id if mine else ""


def changed_lines(diff: str) -> dict[str, list[list[int]]]:
    """Right-side line ranges (inclusive) that a diff adds, per file."""
    out: dict[str, list[list[int]]] = {}
    for path, fd in parse_diff(diff).items():
        ranges: list[list[int]] = []
        for ln in fd.lines:
            if ln.kind != "add" or ln.new is None:
                continue
            if ranges and ranges[-1][1] == ln.new - 1:
                ranges[-1][1] = ln.new
            else:
                ranges.append([ln.new, ln.new])
        if ranges:
            out[path] = ranges
    return out


def hold_back(findings: Sequence[Finding], rnd: dict | None, quiet_after: int) -> tuple[list[Finding], list[Finding]]:
    """Split off suggestions outside the lines changed since our last review, from round `quiet_after` on.

    Critical and warning findings are never held back, wherever they are. Returns (kept, held)."""
    if not rnd or quiet_after <= 0 or rnd.get("number", 0) < quiet_after or rnd.get("changed") is None:
        return list(findings), []
    changed = rnd["changed"]
    kept: list[Finding] = []
    held: list[Finding] = []
    for f in findings:
        inside = any(a <= f.line <= b for a, b in changed.get(f.file, ()))
        (kept if f.severity is not Severity.SUGGESTION or inside else held).append(f)
    return kept, held
