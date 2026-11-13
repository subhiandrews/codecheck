"""The pure part of posting a review: order the aggregated findings and split them into inline and summary-only."""

from dataclasses import dataclass

from codecheck.core.diffmap import Anchor, FileDiff, anchor_finding
from codecheck.core.findings import attach_anchors, finding_id, merge_exact_duplicates
from codecheck.models import Finding


@dataclass(frozen=True, slots=True)
class ReviewPlan:
    inline: list[tuple[Finding, Anchor]]
    not_inline: list[Finding]  # unanchorable findings first, then those past the inline cap
    repeated: int  # already posted inline by an earlier run, so left out of both lists


def finalize_findings(findings: list[Finding], diff_files: dict[str, FileDiff]) -> list[Finding]:
    """Attach diff anchors, merge exact duplicates, and sort most severe first, then by file and line."""
    out = merge_exact_duplicates(attach_anchors(findings, diff_files))
    out.sort(key=lambda f: (-f.severity.rank, f.file, f.line))
    return out


def plan_review(findings: list[Finding], diff_files: dict[str, FileDiff], *, already: set[str], cap: int) -> ReviewPlan:
    anchored, unlisted, repeated = [], [], 0
    for f in findings:
        if finding_id(f) in already:
            repeated += 1  # posted inline by an earlier run: do not post it again
            continue
        a = anchor_finding(diff_files, f.file, f.line)
        if a:
            anchored.append((f, a))
        else:
            unlisted.append(f)
    return ReviewPlan(inline=anchored[:cap], not_inline=unlisted + [f for f, _ in anchored[cap:]], repeated=repeated)
