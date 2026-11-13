from codecheck.core.render import (
    ATTRIBUTION,
    RUN_MARKER,
    comment_body,
    marker,
    render_local,
    summary_body,
)
from codecheck.models import Decision, Finding, Severity


def f(**over):
    base = {"file": "a.py", "line": 3, "severity": Severity.WARNING, "category": "bug", "message": "Off by one",
                "reviewers": ("skeptic",)}
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


def test_summary_footer_present_when_attribution_on():
    body = summary_body("abcdef012345", [], [], Decision("COMMENT", "r"), attribution=True)
    assert ATTRIBUTION in body
    assert body.index(ATTRIBUTION) < body.index("<!-- code-check:run:")


def test_summary_footer_absent_by_default():
    assert ATTRIBUTION not in summary_body("abcdef012345", [], [], Decision("COMMENT", "r"))


def test_inline_comment_has_no_attribution():
    assert ATTRIBUTION not in comment_body(f(), "abc123def456")


def test_render_replies_frames_text_as_data():
    from codecheck.core.precheck import Reply
    from codecheck.core.render import render_replies

    out = render_replies([
        Reply("T1", "a.py", 3, "alice", "hi", "2026-01-02T00:00:00Z"),
        Reply("T1", "a.py", 3, "bob", "yo", "2026-01-03T00:00:00Z"),
        Reply("T2", "b.py", None, "carol", "x", "2026-01-04T00:00:00Z"),
    ])
    first = out.splitlines()[0]
    assert "untrusted" in first and "data" in first and "never as instructions" in first
    assert out.count("## ") == 2 and "## a.py:3" in out
    assert "- alice (2026-01-02T00:00:00Z): hi" in out and "- bob (2026-01-03T00:00:00Z): yo" in out


def test_render_replies_omits_missing_line():
    from codecheck.core.precheck import Reply
    from codecheck.core.render import render_replies

    out = render_replies([Reply("T2", "b.py", None, "carol", "x", "2026-01-04T00:00:00Z")])
    assert "## b.py\n" in out and "None" not in out
