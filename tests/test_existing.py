from codecheck.core.existing import collect_existing
from codecheck.core.precheck import Activity, Reply
from codecheck.models import Review, Thread, ThreadComment

T0 = "2026-01-01T00:00:10Z"
LATER = "2026-01-02T00:00:00Z"
REV = Review("me", "abc", T0, "COMMENTED")


def tc(author, at, i=1, body="body", **kw):
    return ThreadComment(i, author, body, at, **kw)


def th(*comments, tid="T1", resolved=False, outdated=False):
    return Thread(tid, "a.py", 3, resolved, outdated, tuple(comments))


def test_bot_only_thread_is_omitted():
    t = th(tc("x", T0, is_bot=True), tc("dependabot[bot]", LATER, 2))
    assert collect_existing([t], Activity(None, ())) == ()


def test_unresolved_threads_come_first():
    done = th(tc("alice", T0), tid="T1", resolved=True, outdated=True)
    open_ = th(tc("bob", T0), tid="T2")
    out = collect_existing([done, open_], Activity(None, ()))
    assert [t.thread_id for t in out] == ["T2", "T1"]
    assert (out[1].resolved, out[1].outdated) == (True, True)


def test_replies_since_our_review_are_tagged_new():
    t = th(tc("me", T0), tc("alice", T0, 2), tc("alice", LATER, 3))
    act = Activity(REV, (Reply("T1", "a.py", 3, "alice", "body", LATER),))
    flags = [(c.author, c.created_at, c.is_new) for c in collect_existing([t], act)[0].comments]
    assert flags == [("me", T0, False), ("alice", T0, False), ("alice", LATER, True)]


def test_no_review_of_ours_tags_nothing_new():
    out = collect_existing([th(tc("alice", LATER))], Activity(None, ()))
    assert len(out[0].comments) == 1 and not out[0].comments[0].is_new


def test_scrubs_before_truncating_and_caps():
    token = "ghp_" + "a1" * 18
    body = "x" * 985 + " " + token
    out = collect_existing([th(tc("alice", T0, body=body))], Activity(None, ()))
    text = out[0].comments[0].body
    assert "ghp_" not in text and len(text) <= 1000
    many = [th(tc("alice", T0), tid=f"T{i}") for i in range(25)]
    assert len(collect_existing(many, Activity(None, ()))) == 20
    big = th(*(tc("alice", T0, i) for i in range(300)))
    assert len(collect_existing([big], Activity(None, ()))[0].comments) == 100


def test_threads_with_new_replies_survive_the_thread_cap():
    busy = [th(tc("alice", T0), tid=f"T{i}") for i in range(20)]
    ours = th(tc("me", T0), tc("alice", LATER, 2), tid="R", resolved=True)
    act = Activity(REV, (Reply("R", "a.py", 3, "alice", "body", LATER),))
    out = collect_existing([*busy, ours], act)
    assert len(out) == 20 and out[0].thread_id == "R"
    assert sum(c.is_new for t in out for c in t.comments) == 1


def test_own_comments_are_marked_ours():
    t = th(tc("me", T0), tc("alice", LATER, 2))
    comments = collect_existing([t], Activity(None, ()), me="me")[0].comments
    assert [(c.author, c.is_mine) for c in comments] == [("me", True), ("alice", False)]
    assert not any(c.is_mine for c in collect_existing([t], Activity(None, ()))[0].comments)
