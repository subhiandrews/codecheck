from codecheck.core.state import (
    FileLock, StateStore, WorkItem, actionable_threads, babysit_work, local_fingerprint, mark_processed, select_work,
)
from codecheck.models import Check, PRRef, PullRequest, Thread, ThreadComment

REF = PRRef("o/r", 7)


def pr(head="aaa", state="open"):
    return PullRequest(REF, "t", "", "me", "main", "feat", head, False, state, "u")


def thread(tid, last_author, resolved=False, outdated=False):
    c = ThreadComment(1, last_author, "body", "2026-01-01")
    return Thread(tid, "a.py", 3, resolved, outdated, (ThreadComment(0, "me", "first", "x"), c))


def test_store_roundtrip_incr_and_atomic(tmp_path):
    s = StateStore(tmp_path / "s" / "state.json")
    assert s.get("k") is None
    s.set("k", "v")
    assert StateStore(tmp_path / "s" / "state.json").get("k") == "v"
    assert s.incr("n") == 1 and s.incr("n") == 2
    s.delete("n")
    s.delete("never-existed")
    assert s.get("n") is None
    assert not list((tmp_path / "s").glob("*.tmp"))


def test_corrupt_state_is_quarantined_not_fatal(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    s = StateStore(path)
    assert s.get("k") is None
    s.set("k", 1)
    assert s.get("k") == 1
    assert (tmp_path / "state.corrupt").exists()


def test_file_lock_blocks_second_holder_until_released_or_stale(tmp_path):
    path = tmp_path / "loop.lock"
    a, b = FileLock(path), FileLock(path)
    assert a.acquire() and not b.acquire()
    a.release()
    assert b.acquire()
    b.release()
    FileLock(path).acquire()
    stale = FileLock(path, stale_after=10, now=lambda: path.stat().st_mtime + 100)
    assert stale.acquire()


def test_local_fingerprint_changes_with_any_input():
    base = local_fingerprint("mb", "head", "diff")
    assert base == local_fingerprint("mb", "head", "diff")
    assert len({base, local_fingerprint("mb2", "head", "diff"), local_fingerprint("mb", "head2", "diff"),
                local_fingerprint("mb", "head", "diff2")}) == 4


def test_actionable_threads_skips_resolved_outdated_and_my_last_word():
    ts = [thread("t1", "alice"), thread("t2", "alice", resolved=True), thread("t3", "alice", outdated=True), thread("t4", "me")]
    assert [t.id for t in actionable_threads(ts, "me")] == ["t1"]


def test_babysit_work_none_when_idle_and_fingerprint_tracks_state():
    assert babysit_work(pr(), [], [Check("ci", "pass")], "me") is None
    assert babysit_work(pr(state="closed"), [thread("t1", "alice")], [], "me") is None
    a = babysit_work(pr(), [thread("t1", "alice")], [], "me")
    b = babysit_work(pr(head="bbb"), [thread("t1", "alice")], [], "me")
    c = babysit_work(pr(), [], [Check("ci", "fail")], "me")
    assert a and b and c and len({a.fingerprint, b.fingerprint, c.fingerprint}) == 3
    assert "1 thread" in a.reason and "failing" in c.reason


def test_select_work_skips_processed_caps_and_does_not_starve(tmp_path):
    s = StateStore(tmp_path / "s.json")
    items = [WorkItem(PRRef("o/r", n), f"fp{n}", "x") for n in range(1, 9)]
    first = select_work(items, s, "review", 5)
    assert [i.ref.number for i in first] == [1, 2, 3, 4, 5]
    for i in first:  # even PRs that failed are marked processed, so they cannot starve the queue
        mark_processed(s, "review", i)
    assert [i.ref.number for i in select_work(items, s, "review", 5)] == [6, 7, 8]
    changed = [WorkItem(PRRef("o/r", 1), "fp1-new", "x"), *items[1:]]
    assert select_work(changed, s, "review", 5)[0].ref.number == 1
    assert select_work(items, s, "babysit", 5)[0].ref.number == 1  # modes are independent
