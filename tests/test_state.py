import os
import stat
import subprocess
import sys
import threading
import time

import pytest

from codecheck.core.state import (
    MAX_LOOP_FAILURES,
    FileLock,
    StateStore,
    WorkItem,
    actionable_threads,
    babysit_work,
    ensure_private_dir,
    local_fingerprint,
    mark_processed,
    record_failure,
    select_work,
)
from codecheck.errors import CodeCheckError
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


def test_release_by_non_owner_and_double_release_are_noops(tmp_path):
    path = tmp_path / "loop.lock"
    a, b = FileLock(path), FileLock(path)
    assert a.acquire()
    b.release()  # never acquired
    assert path.exists()
    a.release()
    assert not path.exists()
    assert b.acquire()
    a.release()  # double release by old owner must not delete b's lock
    assert path.exists()
    b.release()
    b.release()
    assert not path.exists()


def test_reclaimed_holder_release_does_not_delete_new_holders_lock(tmp_path):
    path = tmp_path / "loop.lock"
    old = FileLock(path)
    assert old.acquire()
    new = FileLock(path, stale_after=10, now=lambda: path.stat().st_mtime + 100)
    assert new.acquire()
    old.release()
    assert path.exists()


def test_stale_reclaim_race_loser_does_not_take_fresh_lock(tmp_path):
    path = tmp_path / "loop.lock"
    FileLock(path).acquire()
    winner = FileLock(path)
    swapped = []

    def clock():
        # Between the loser's stat and its rename, another process reclaims and re-acquires.
        if not swapped:
            swapped.append(True)
            path.unlink()
            assert winner.acquire()
        return path.stat().st_mtime + 100

    loser = FileLock(path, stale_after=10, now=clock)
    assert not loser.acquire()
    assert path.read_text() == winner._token
    assert not list(tmp_path.glob("*.reclaim.*"))
    winner.release()
    assert not path.exists()


def test_concurrent_writers_do_not_lose_updates(tmp_path):
    path = tmp_path / "s" / "state.json"

    def worker(n):
        store = StateStore(path)
        for _ in range(25):
            store.incr("count")
            store.set(f"w{n}", n)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    s = StateStore(path)
    assert s.get("count") == 100 and [s.get(f"w{n}") for n in range(4)] == [0, 1, 2, 3]
    assert not (tmp_path / "s" / "state.json.lock").exists()


def test_state_write_times_out_clearly_when_the_lock_is_held(tmp_path):
    path = tmp_path / "state.json"
    holder = FileLock(tmp_path / "state.json.lock")
    assert holder.acquire()
    with pytest.raises(CodeCheckError, match="locked"):
        StateStore(path, lock_timeout=0.1).set("k", 1)
    holder.release()
    StateStore(path).set("k", 1)


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_lock_held_by_a_dead_process_is_reclaimed_even_when_fresh(tmp_path):
    path = tmp_path / "loop.lock"
    path.write_text(f"{_dead_pid()}:x")
    lock = FileLock(path)
    assert lock.acquire() and path.read_text() == lock._token


def test_lock_held_by_a_live_process_is_kept_until_the_age_limit(tmp_path):
    path = tmp_path / "loop.lock"
    path.write_text(f"{os.getpid()}:x")
    old = time.time() - 3 * 3600
    os.utime(path, (old, old))
    assert not FileLock(path, stale_after=4 * 3600).acquire()
    assert FileLock(path, stale_after=2 * 3600).acquire()


def test_touch_and_keep_alive_refresh_only_our_own_lock(tmp_path):
    path = tmp_path / "loop.lock"
    a = FileLock(path)
    assert not a.touch()  # not held
    assert a.acquire()
    os.utime(path, (1, 1))
    assert a.touch() and path.stat().st_mtime > time.time() - 60
    os.utime(path, (1, 1))
    with a.keep_alive(interval=0.05):
        time.sleep(0.3)
    assert path.stat().st_mtime > time.time() - 60
    path.write_text("other:token")
    assert not a.touch()


def test_record_failure_counts_per_fingerprint_and_gives_up(tmp_path):
    s = StateStore(tmp_path / "s.json")
    item = WorkItem(REF, "fp1", "")
    results = [record_failure(s, "review", item) for _ in range(MAX_LOOP_FAILURES)]
    assert results == [(1, False), (2, False), (MAX_LOOP_FAILURES, True)]
    assert select_work([item], s, "review", 5) == []
    assert s.get("review:o/r#7") is None  # loop bookkeeping never claims the head was reviewed
    moved = WorkItem(REF, "fp2", "")
    assert select_work([moved], s, "review", 5) == [moved]
    assert record_failure(s, "review", moved) == (1, False)


def test_state_dir_is_created_owner_only(tmp_path):
    d = ensure_private_dir(tmp_path / "a" / "state")
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    StateStore(tmp_path / "b" / "state.json").set("k", 1)
    assert stat.S_IMODE((tmp_path / "b").stat().st_mode) == 0o700
