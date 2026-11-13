from codecheck.core.precheck import precheck_local, precheck_pr
from codecheck.core.state import StateStore
from codecheck.models import PRRef, PullRequest

REF = PRRef("o/r", 7)


def pr(**over):
    base = {"ref": REF, "title": "t", "body": "", "author": "alice", "base_ref": "main", "head_ref": "feat",
                "head_sha": "abc123", "is_draft": False, "state": "open", "url": "u"}
    return PullRequest(**{**base, **over})


def test_pr_precheck_reasons(tmp_path):
    s = StateStore(tmp_path / "s.json")
    kw = {"ignore": ("**/*.lock",), "me": "me"}
    assert not precheck_pr(pr(state="closed"), ["a.py"], s, **kw).proceed
    assert "draft" in precheck_pr(pr(is_draft=True), ["a.py"], s, **kw).reason
    assert "own PR" in precheck_pr(pr(author="me"), ["a.py"], s, **kw).reason
    assert "no reviewable" in precheck_pr(pr(), ["x.lock"], s, **kw).reason
    ok = precheck_pr(pr(), ["a.py", "x.lock"], s, **kw)
    assert ok.proceed and ok.files == ("a.py",) and ok.fingerprint == "abc123"


def test_pr_precheck_skips_already_reviewed_head_unless_forced(tmp_path):
    s = StateStore(tmp_path / "s.json")
    s.set(f"review:{REF}", "abc123")
    kw = {"ignore": (), "me": "me"}
    assert "already reviewed" in precheck_pr(pr(), ["a.py"], s, **kw).reason
    assert precheck_pr(pr(), ["a.py"], s, force=True, **kw).proceed
    assert precheck_pr(pr(head_sha="def456"), ["a.py"], s, **kw).proceed


def test_local_precheck(tmp_path):
    s = StateStore(tmp_path / "s.json")
    assert "no changes" in precheck_local([], "fp", "local:k", s, ignore=()).reason
    assert "no reviewable" in precheck_local(["x.lock"], "fp", "local:k", s, ignore=("**/*.lock",)).reason
    assert precheck_local(["a.py"], "fp", "local:k", s, ignore=()).proceed
    s.set("local:k", "fp")
    assert "unchanged" in precheck_local(["a.py"], "fp", "local:k", s, ignore=()).reason
    assert precheck_local(["a.py"], "fp2", "local:k", s, ignore=()).proceed
    assert precheck_local(["a.py"], "fp", "local:k", s, ignore=(), force=True).proceed


# --- review verdict from GitHub activity ---

from codecheck.core.precheck import (
    Activity,
    Reply,
    cap_replies,
    needs_activity,
    review_activity,
    review_verdict,
)
from codecheck.models import Review, Thread, ThreadComment

T0 = "2026-01-01T00:00:10Z"


def tc(author, at, i=1, **kw):
    return ThreadComment(i, author, "body", at, **kw)


def th(*comments, tid="T1"):
    return Thread(tid, "a.py", 3, False, False, tuple(comments))


def test_activity_ignores_own_bot_and_foreign_threads():
    reviews = [Review("me", "abc", T0, "COMMENTED")]
    later = "2026-01-02T00:00:00Z"
    threads = [
        th(tc("me", T0), tc("me", later, 2), tc("dependabot[bot]", later, 3), tc("x", later, 4, is_bot=True)),
        th(tc("alice", T0), tc("bob", later, 2), tid="T2"),
    ]
    assert review_activity(reviews, threads, "me").replies == ()


def test_activity_counts_only_replies_after_the_review():
    reviews = [Review("me", "abc", T0, "COMMENTED")]
    threads = [th(tc("me", T0), tc("alice", T0, 2), tc("alice", "2026-01-01T00:00:11Z", 3))]
    replies = review_activity(reviews, threads, "me").replies
    assert [r.created_at for r in replies] == ["2026-01-01T00:00:11Z"]
    assert replies[0].thread_id == "T1" and replies[0].path == "a.py" and replies[0].author == "alice"


def test_activity_picks_latest_own_review():
    reviews = [
        Review("me", "old", "2026-01-01T00:00:00Z", "COMMENTED"),
        Review("me", "new", "2026-01-03T00:00:00Z", "COMMENTED"),
        Review("ghost", "zzz", "2026-01-05T00:00:00Z", "APPROVED"),
    ]
    act = review_activity(reviews, [], "me")
    assert act.last_review.commit_id == "new"
    assert review_activity([Review("x", "a", T0, "COMMENTED")], [th(tc("me", T0), tc("a", "2027"))], "me") == Activity(None, ())


def reply(i=1, tid="T1", body="b"):
    return Reply(tid, "a.py", 3, "alice", body, "2026-01-02T00:00:00Z")


def test_verdict_table(tmp_path):
    s = StateStore(tmp_path / "s.json")
    rev = Review("me", "abc123", T0, "COMMENTED")
    two = (reply(), reply())
    # no review anywhere
    assert review_verdict(pr(), s, Activity(None, ())).kind == "new"
    assert review_verdict(pr(), s, None).kind == "new"
    # local state absent, our GitHub review at head
    assert review_verdict(pr(), s, Activity(rev, ())).kind == "reviewed"
    # empty commit_id is not a usable review
    assert review_verdict(pr(), s, Activity(Review("me", "", T0, "COMMENTED"), ())).kind == "new"
    s.set(f"review:{REF}", "abc123")
    v = review_verdict(pr(), s, Activity(rev, ()))
    assert v.kind == "reviewed" and "already reviewed" in v.reason
    assert review_verdict(pr(), s, None).kind == "reviewed"
    v = review_verdict(pr(), s, Activity(rev, two))
    assert v.kind == "replies" and "2 replies on 1 of your threads" in v.reason and v.replies == two
    v = review_verdict(pr(), s, Activity(rev, (reply(),)))
    assert "1 reply on 1 of your threads" in v.reason
    v = review_verdict(pr(head_sha="def456"), s, Activity(rev, two))
    assert v.kind == "new" and "new commits" in v.reason
    s.set(f"review-incomplete:{REF}", "abc123")
    assert review_verdict(pr(), s, Activity(rev, ())).kind == "retry"


def test_needs_activity(tmp_path):
    s = StateStore(tmp_path / "s.json")
    assert needs_activity(pr(), s)
    s.set(f"review:{REF}", "abc123")
    assert needs_activity(pr(), s)
    assert not needs_activity(pr(head_sha="def"), s)
    s.set(f"review-incomplete:{REF}", "abc123")
    assert not needs_activity(pr(), s)


def test_cap_replies_limits_threads_chars_and_scrubs():
    many = [reply(tid=f"T{i}") for i in range(25)]
    assert len(cap_replies(many)) == 20
    assert len(cap_replies([reply(body="x" * 2000)])[0].body) == 1000
    token = "ghp_" + "a1" * 18
    assert token not in cap_replies([reply(body=f"key {token} end")])[0].body


def test_cap_replies_scrubs_before_truncating():
    token = "ghp_" + "a1" * 18
    body = "x" * 985 + " " + token
    out = cap_replies([reply(body=body)])[0].body
    assert "ghp_" not in out and len(out) <= 1000
