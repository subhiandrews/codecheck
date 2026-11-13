import pytest

from codecheck.core.policy import decide
from codecheck.models import Finding, Severity


def f(sev):
    return Finding("a.py", 1, sev, "c", "m")


def test_approves_only_when_enabled_and_no_blockers():
    assert decide([f(Severity.SUGGESTION)], auto_approve=True, is_draft=False).event == "APPROVE"
    assert decide([], auto_approve=True, is_draft=False).event == "APPROVE"


@pytest.mark.parametrize("sev", [Severity.CRITICAL, Severity.WARNING])
def test_blockers_prevent_approval(sev):
    d = decide([f(sev)], auto_approve=True, is_draft=False)
    assert d.event == "COMMENT" and "1" in d.reason


def test_auto_approve_off_never_approves():
    assert decide([], auto_approve=False, is_draft=False).event == "COMMENT"


def test_draft_and_incomplete_never_approve():
    assert decide([], auto_approve=True, is_draft=True).event == "COMMENT"
    d = decide([], auto_approve=True, is_draft=False, incomplete=True)
    assert d.event == "COMMENT" and "incomplete" in d.reason
