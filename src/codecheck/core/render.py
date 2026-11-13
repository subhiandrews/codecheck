"""Comment, summary and local-report rendering. Hidden markers let shadow collection find our comments."""

import re
from collections import Counter
from collections.abc import Sequence

from codecheck.models import Decision, Finding, Severity

RUN_MARKER = "<!-- code-check:run:{} -->"
FINDING_MARKER = re.compile(r"<!-- code-check:finding:([0-9a-f]{12}) -->")


def marker(fid: str) -> str:
    return f"<!-- code-check:finding:{fid} -->"


def _fence(text: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def comment_body(f: Finding, fid: str, *, relocated: bool = False) -> str:
    lines = [f"**{f.severity.label}** · {f.category}", "", f.message]
    if f.suggestion:
        text = f.suggestion.rstrip("\n")
        fence = _fence(text)
        if relocated:
            lines += ["", "Suggested change:", fence, text, fence]
        else:
            lines += ["", f"{fence}suggestion", text, fence]
    if f.reviewers:
        lines += ["", f"<sub>code-check · {', '.join(f.reviewers)}</sub>"]
    lines.append(marker(fid))
    return "\n".join(lines)


def summary_body(
    run_id: str,
    findings: Sequence[Finding],
    unlisted: Sequence[Finding],
    decision: Decision,
    *,
    base_warning: str | None = None,
    failed_reviewers: Sequence[str] = (),
) -> str:
    counts = Counter(f.severity for f in findings)
    parts = [f"{counts[s]} {s.value}" for s in (Severity.CRITICAL, Severity.WARNING, Severity.SUGGESTION) if counts[s]]
    lines = ["## code-check review", "", ", ".join(parts) if parts else "No issues found."]
    if failed_reviewers:
        lines += ["", f"> Incomplete: no usable output from {', '.join(failed_reviewers)}; this review is not approved."]
    if base_warning:
        lines += ["", f"> {base_warning}"]
    if unlisted:
        lines += ["", "### Findings without an inline location", ""]
        lines += [f"- **{f.severity.label}** `{f.file}:{f.line}` {f.message}" for f in unlisted]
    lines += ["", RUN_MARKER.format(run_id)]
    return "\n".join(lines)


def render_local(new: Sequence[Finding], unchanged: int, base_warning: str | None = None) -> str:
    lines = [f"code-check local review: {len(new)} new, {unchanged} already shown"]
    if base_warning:
        lines.append(f"note: {base_warning}")
    for f in new:
        lines.append(f"[{f.severity.label}] {f.file}:{f.line} ({f.category}) {f.message}")
        if f.suggestion:
            lines += ["    suggestion:", *(f"      {s}" for s in f.suggestion.splitlines())]
    return "\n".join(lines)
