from codecheck.core.render import RUN_MARKER, comment_body, marker, render_local, summary_body
from codecheck.models import Decision, Finding, Severity


def f(**over):
    base = dict(file="a.py", line=3, severity=Severity.WARNING, category="bug", message="Off by one",
                reviewers=("skeptic",))
    return Finding(**{**base, **over})


def test_comment_body_has_label_marker_and_attribution():
    body = comment_body(f(), "abc123def456")
    assert body.startswith("**Warning** · bug")
    assert "Off by one" in body and "skeptic" in body and marker("abc123def456") in body


def test_suggestion_block_vs_relocated_plain_block_and_fence_escalation():
    assert "```suggestion\nx = 1\n```" in comment_body(f(suggestion="x = 1"), "i")
    relocated = comment_body(f(suggestion="x = 1"), "i", relocated=True)
    assert "```suggestion" not in relocated and "x = 1" in relocated
    tricky = comment_body(f(suggestion="print('```')"), "i")
    assert "````suggestion" in tricky


def test_summary_counts_notes_and_unlisted():
    body = summary_body(
        "abcdef012345", [f(), f(severity=Severity.CRITICAL, line=9), f(severity=Severity.SUGGESTION, line=11)],
        [f(line=99, message="not on the diff")], Decision("COMMENT", "r"),
        base_warning="base may be stale", failed_reviewers=("skeptic",),
    )
    assert "1 critical, 1 warning, 1 suggestion" in body
    assert "base may be stale" in body and "skeptic" in body and "not approved" in body
    assert "`a.py:99`" in body and RUN_MARKER.format("abcdef012345") in body


def test_summary_when_clean():
    assert "No issues found." in summary_body("abcdef012345", [], [], Decision("APPROVE", "r"))


def test_render_local():
    text = render_local([f(suggestion="y")], 2, "base may be stale")
    assert "1 new, 2 already shown" in text and "[Warning] a.py:3 (bug) Off by one" in text
    assert "base may be stale" in text and "y" in text
