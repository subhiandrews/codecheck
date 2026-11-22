"""Phase 3: automatic outcome collection (schedule, autocollect, the cleanup hook, `memory collect`) and verdict roles."""

import time
from dataclasses import replace

import pytest
from clihelp import call, make_ctx, pr_setup, run_pr_context
from fakes import FakeProvider
from gitutil import make_origin_and_clone

from codecheck.config import MemoryConfig
from codecheck.memory import schedule
from codecheck.memory.autocollect import collect_due
from codecheck.memory.consolidate import DAY, Params, consolidate
from codecheck.memory.db import MemoryDB
from codecheck.memory.evidence import load_runs
from codecheck.models import PRRef, Thread, ThreadComment
from codecheck.shadow.store import ShadowStore

pytestmark = pytest.mark.usefixtures("git_env")

T0 = 1_700_000_000.0
FID = "a" * 12


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def rid_of(name):
    return "".join(f"{ord(c):02x}" for c in name).ljust(12, "0")[:12]


def seed_review(shadow, clock, name, number, *, at, repo="o/r", fid=FID, path="src/app/a.py"):
    """A posted review at `at`: a run, one finding and the decision."""
    clock.t = at
    rid = rid_of(name)
    shadow.append(rid, {"type": "run", "repo": repo, "pr": f"{repo}#{number}", "tool_version": "1", "pr_author": "pat"})
    shadow.append(rid, {"type": "finding", "id": fid, "file": path, "category": "tests", "message": "m", "severity": "Warning"})
    shadow.append(rid, {"type": "decision", "event": "COMMENT", "reason": "r", "incomplete": False})
    return rid


def thread(tid, fid=FID, path="src/app/a.py", reply_by="maya", assoc="OWNER"):
    root = ThreadComment(1, "me", f"**Warning**\n<!-- code-check:finding:{fid} -->", "t")
    reply = ThreadComment(2, reply_by, "no, this is wrong", "t", association=assoc)
    return Thread(tid, path, 3, False, False, (root, reply))


@pytest.fixture
def world(tmp_path):
    _, clone = make_origin_and_clone(tmp_path)
    provider = FakeProvider()
    ctx = make_ctx(tmp_path, clone, provider=provider, memory=MemoryConfig(collect=True, store=str(tmp_path / "mem")))
    clock = Clock()
    ctx.shadow = ShadowStore(tmp_path / "shadow", now=clock)
    return ctx, provider, clock, tmp_path


def test_a_review_is_due_after_each_milestone_once(world):
    ctx, _p, clock, _tmp = world
    seed_review(ctx.shadow, clock, "a1", 1, at=T0)
    state = ctx.state

    def due_at(days):
        return [c.pr for c in schedule.due(state, ctx.shadow, T0 + days * DAY)]

    assert due_at(0.5) == []
    (cand,) = schedule.due(state, ctx.shadow, T0 + 1 * DAY)
    assert cand.pr == "o/r#1" and cand.milestone == 0
    schedule.mark_collected(state, cand, T0 + DAY)
    assert due_at(2) == [] and due_at(3) == ["o/r#1"]
    (cand,) = schedule.due(state, ctx.shadow, T0 + 20 * DAY)  # a long gap jumps to the newest milestone, one collection
    assert cand.milestone == 3
    schedule.mark_collected(state, cand, T0 + 20 * DAY)
    assert due_at(30) == []  # all four done
    assert state.get("memory:collect:o/r#1")["done"] == 3
    assert not [k for k in state._load() if k.startswith("review:")]


def test_a_newer_review_restarts_the_schedule(world):
    ctx, _p, clock, _tmp = world
    seed_review(ctx.shadow, clock, "a1", 1, at=T0)
    (cand,) = schedule.due(ctx.state, ctx.shadow, T0 + 2 * DAY)
    schedule.mark_collected(ctx.state, cand, T0 + 2 * DAY)
    seed_review(ctx.shadow, clock, "a2", 1, at=T0 + 3 * DAY)
    assert schedule.due(ctx.state, ctx.shadow, T0 + 3.5 * DAY) == []
    assert [c.pr for c in schedule.due(ctx.state, ctx.shadow, T0 + 4 * DAY)] == ["o/r#1"]


def test_collection_stops_seven_days_after_the_pr_closes(world):
    ctx, provider, clock, _tmp = world
    seed_review(ctx.shadow, clock, "a1", 1, at=T0)
    provider.add_pr(1, state="merged")
    provider.threads[PRRef("o/r", 1)] = [thread("T1")]
    first = collect_due(ctx, now=Clock(T0 + 1 * DAY))
    assert first["collected"] == ["o/r#1"]
    assert ctx.state.get("memory:collect:o/r#1")["closed_at"] == T0 + 1 * DAY  # first seen closed
    assert collect_due(ctx, now=Clock(T0 + 3 * DAY))["due"] == ["o/r#1"]  # 2 days after closing: still collected
    assert collect_due(ctx, now=Clock(T0 + 9 * DAY))["due"] == []  # 8 days after closing: stopped
    assert collect_due(ctx, now=Clock(T0 + 9 * DAY), everything=True)["due"] == ["o/r#1"]  # --all ignores the schedule


def test_at_most_three_prs_per_call_newest_review_first(world):
    ctx, provider, clock, _tmp = world
    for n in range(1, 6):
        seed_review(ctx.shadow, clock, f"r{n}", n, at=T0 + n * 60, fid=f"{n:012x}")
        provider.add_pr(n)
    out = collect_due(ctx, now=Clock(T0 + 2 * DAY))
    assert out["due"] == [f"o/r#{n}" for n in (5, 4, 3, 2, 1)] and out["collected"] == ["o/r#5", "o/r#4", "o/r#3"]
    again = collect_due(ctx, now=Clock(T0 + 2 * DAY))
    assert again["collected"] == ["o/r#2", "o/r#1"]  # the rest on the next call
    assert collect_due(ctx, now=Clock(T0 + 2 * DAY))["collected"] == []


def test_the_wall_budget_stops_between_prs():
    cands = [schedule.Candidate(f"o/r#{n}", T0, 0) for n in range(3)]
    ticks = iter([0.0, 4.0, 11.0, 12.0])
    done = []
    attempted = schedule.run_budgeted(cands, lambda c: done.append(c.pr), lambda: next(ticks))
    assert [c.pr for c in attempted] == ["o/r#0", "o/r#1"] and done == ["o/r#0", "o/r#1"]


def test_a_provider_error_is_swallowed_and_retried_later(world):
    ctx, provider, clock, _tmp = world
    seed_review(ctx.shadow, clock, "a1", 1, at=T0)
    provider.add_pr(1)

    def boom(ref, reactions=True):
        raise RuntimeError("host down")

    provider.list_threads = boom
    out = collect_due(ctx, now=Clock(T0 + 2 * DAY))
    assert out["collected"] == [] and "host down" in out["errors"][0]["error"] and out["consolidated"] is False
    entry = ctx.state.get("memory:collect:o/r#1")  # not marked collected: due again once the backoff passes
    assert entry["done"] == -1 and entry["failures"] == 1
    provider.whoami = boom
    assert "errors" in collect_due(ctx, now=Clock(T0 + 2 * DAY))  # even a failing whoami never raises


def test_collection_records_outcomes_and_consolidates_once_and_is_idempotent(world):
    ctx, provider, clock, tmp = world
    seed_review(ctx.shadow, clock, "a1", 1, at=T0)
    provider.add_pr(1)
    provider.threads[PRRef("o/r", 1)] = [thread("T1")]
    now = Clock(T0 + 2 * DAY)
    out = collect_due(ctx, now=now)
    assert out["collected"] == ["o/r#1"] and out["consolidated"] is True
    assert any(r.get("type") == "outcome" for r in ctx.shadow.read(rid_of("a1")))
    rows = MemoryDB(tmp / "mem").evidence()
    assert [(e.signal, e.role) for e in rows] == [("reply", "maintainer")]
    assert list(ctx.state._load()) == ["memory:collect:o/r#1"]
    assert collect_due(ctx, now=now) == {"due": [], "collected": [], "errors": [], "consolidated": False}
    assert len(MemoryDB(tmp / "mem").evidence()) == 1


def test_dry_run_lists_and_writes_nothing(world):
    ctx, _provider, clock, tmp = world
    seed_review(ctx.shadow, clock, "a1", 1, at=T0)
    out = collect_due(ctx, now=Clock(T0 + 2 * DAY), dry_run=True)
    assert out["due"] == ["o/r#1"] and out["collected"] == []
    assert ctx.state.get("memory:collect:o/r#1") is None and not (tmp / "mem").exists()


def test_no_shadow_store_means_nothing_to_do(world):
    ctx, *_ = world
    ctx.shadow = None
    assert collect_due(ctx)["due"] == []


def _cleanup_world(tmp_path, collect):
    ctx, provider, pr = pr_setup(tmp_path, memory=MemoryConfig(enabled=collect, collect=collect, store=str(tmp_path / "mem")))
    out = run_pr_context(ctx, pr)
    clock = Clock(time.time() - 3 * DAY)
    ctx.shadow = ShadowStore(tmp_path / "shadow", now=clock)
    seed_review(ctx.shadow, clock, "old", 9, at=clock.t)  # an earlier review of another PR, due now
    provider.add_pr(9)
    provider.threads[PRRef("o/r", 9)] = [thread("T9")]
    return ctx, out


def test_cleanup_output_is_identical_whether_or_not_collection_runs(tmp_path):
    for sub in ("off", "on"):
        (tmp_path / sub).mkdir()
    off_ctx, off_out = _cleanup_world(tmp_path / "off", False)
    on_ctx, on_out = _cleanup_world(tmp_path / "on", True)
    code_off, res_off = call(off_ctx, "cleanup", "--run", off_out["run_id"])
    code_on, res_on = call(on_ctx, "cleanup", "--run", on_out["run_id"])
    assert (code_off, res_off) == (code_on, res_on) == (0, {"removed": True, "run_dir_removed": True})
    assert not (tmp_path / "off" / "mem").exists() and not any(
        k.startswith("memory:collect:") for k in off_ctx.state._load())
    assert [e.signal for e in MemoryDB(tmp_path / "on" / "mem").evidence()] == ["reply"]  # collected and consolidated
    assert [k for k in on_ctx.state._load() if k.startswith("memory:collect:")] == ["memory:collect:o/r#9"]


def test_cleanup_is_unchanged_when_collection_blows_up(tmp_path, monkeypatch):
    ctx, out = _cleanup_world(tmp_path, True)

    def boom(*a, **k):
        raise RuntimeError("nope")

    monkeypatch.setattr("codecheck.commands.cleanup.collect_due", boom)
    code, res = call(ctx, "cleanup", "--run", out["run_id"])
    assert (code, res) == (0, {"removed": True, "run_dir_removed": True})


def test_memory_collect_command_dry_run_and_all(world):
    ctx, provider, clock, tmp = world
    ctx.config = replace(ctx.config, memory=MemoryConfig(collect=False, store=str(tmp / "mem")))  # explicit run ignores it
    seed_review(ctx.shadow, clock, "a1", 1, at=time.time() - 30 * DAY)
    provider.add_pr(1)
    provider.threads[PRRef("o/r", 1)] = [thread("T1")]
    code, out = call(ctx, "memory", "collect", "--dry-run")
    assert code == 0 and out["due"] == ["o/r#1"] and out["collected"] == []
    code, out = call(ctx, "memory", "collect")
    assert out["collected"] == ["o/r#1"]
    code, out = call(ctx, "memory", "collect", "--all", "--dry-run")
    assert out["due"] == ["o/r#1"]


# Verdict roles --------------------------------------------------------------------------------------------------

def seed_verdict(shadow, clock, name, number, *, verdict, replies, path="src/app/a.py", fid=FID, at=T0):
    rid = seed_review(shadow, clock, name, number, at=at, fid=fid, path=path)
    tid = f"T-{name}"
    shadow.append(rid, {"type": "outcome", "finding_id": fid, "thread_id": tid, "resolved": False, "outdated": False,
                        "replies": replies})
    shadow.append(rid, {"type": "verdict", "thread_id": tid, "verdict": verdict})
    return rid


def reply(role):
    rec = {"author": "x", "body": "this is wrong"}
    return rec | ({"role": role} if role else {})


def lessons_for(shadow, now):
    items = [e for r in load_runs(shadow) for e in r.evidence]
    return items, consolidate(items, now, Params())


def test_a_verdict_takes_the_role_of_whoever_replied_and_a_maintainer_dispute_activates_with_a_second_pr(tmp_path):
    clock = Clock()
    shadow = ShadowStore(tmp_path, now=clock)
    seed_verdict(shadow, clock, "a1", 1, verdict="incorrect", replies=[reply("author"), reply("maintainer")], at=T0)
    seed_verdict(shadow, clock, "a2", 2, verdict="incorrect", replies=[reply("maintainer")], at=T0 + DAY)
    items, lessons = lessons_for(shadow, T0 + 2 * DAY)
    assert {(e.signal, e.role) for e in items} == {("verdict_incorrect", "maintainer")}  # the author's reply does not hide it
    (lesson,) = lessons
    assert lesson.status == "active" and lesson.lean == "dispute"
    one, one_lessons = lessons_for(ShadowStore(tmp_path / "x"), T0)
    assert one == [] and one_lessons == []


def test_one_maintainer_dispute_alone_is_not_enough(tmp_path):
    clock = Clock()
    shadow = ShadowStore(tmp_path, now=clock)
    seed_verdict(shadow, clock, "a1", 1, verdict="incorrect", replies=[reply("maintainer")])
    (lesson,) = lessons_for(shadow, T0 + DAY)[1]
    assert lesson.status != "active"


def test_author_only_disputes_never_activate(tmp_path):
    clock = Clock()
    shadow = ShadowStore(tmp_path, now=clock)
    for n in (1, 2, 3):
        seed_verdict(shadow, clock, f"a{n}", n, verdict="incorrect", replies=[reply("author")], at=T0 + n * DAY)
    items, lessons = lessons_for(shadow, T0 + 5 * DAY)
    assert {e.role for e in items} == {"author"} and lessons == []  # weight 0 teaches nothing


def test_a_verdict_on_a_thread_with_no_replies_is_the_tool_users_own_word(tmp_path):
    clock = Clock()
    shadow = ShadowStore(tmp_path, now=clock)
    seed_verdict(shadow, clock, "a1", 1, verdict="incorrect", replies=[])
    seed_verdict(shadow, clock, "a2", 2, verdict="incorrect", replies=[], at=T0 + DAY)
    items, lessons = lessons_for(shadow, T0 + 2 * DAY)
    assert {e.role for e in items} == {"self"}
    assert all(le.status != "active" for le in lessons)  # 0.5 + 0.5 with no maintainer behind it


def test_old_records_without_roles_still_load_as_unknown(tmp_path):
    clock = Clock()
    shadow = ShadowStore(tmp_path, now=clock)
    seed_verdict(shadow, clock, "a1", 1, verdict="incorrect", replies=[reply(None)])
    (e,) = lessons_for(shadow, T0 + DAY)[0]
    assert (e.signal, e.role) == ("verdict_incorrect", "unknown")
