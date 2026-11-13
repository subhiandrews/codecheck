"""Approval policy. Lives in code, never in a prompt."""

from collections.abc import Sequence

from codecheck.models import Decision, Finding, Severity

_BLOCKING = (Severity.CRITICAL, Severity.WARNING)


def decide(findings: Sequence[Finding], *, auto_approve: bool, is_draft: bool, incomplete: bool = False) -> Decision:
    if is_draft:
        return Decision("COMMENT", "draft PRs are never approved")
    if incomplete:
        return Decision("COMMENT", "review incomplete: at least one reviewer failed or returned invalid output")
    if not auto_approve:
        return Decision("COMMENT", "auto_approve is off")
    blocking = [f for f in findings if f.severity in _BLOCKING]
    if blocking:
        return Decision("COMMENT", f"{len(blocking)} critical/warning finding(s) outstanding")
    return Decision("APPROVE", "no critical or warning findings")
