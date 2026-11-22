"""Review fixes for memory collection: failure backoff, memory-reading runs are not evidence, shadow-off fallback."""

from dataclasses import replace

import pytest
from clihelp import make_ctx
from fakes import FakeProvider
from gitutil import make_origin_and_clone
from test_memory_collect import DAY, T0, Clock, seed_review, world  # noqa: F401

from codecheck.config import MemoryConfig
from codecheck.memory import schedule
from codecheck.memory.autocollect import collect_due
from codecheck.memory.evidence import load_runs
from codecheck.shadow.store import ShadowStore

pytestmark = pytest.mark.usefixtures("git_env")


def test_a_failing_pr_backs_off_and_does_not_starve_the_others(world):  # noqa: F811
    ctx, provider, clock, _tmp = world
    for n in range(1, 5):
        seed_review(ctx.shadow, clock, f"a{n}", n, at=T0 + n)  # PR 4 is newest; 2-4 have no PR on the host
    provider.add_pr(1)
    ok = provider.list_threads

    def broken(ref, reactions=True):
        if ref.number == 1:
            return ok(ref, reactions)
        raise RuntimeError("no access")

    provider.list_threads = broken
    now = T0 + 2 * DAY
    first = collect_due(ctx, now=Clock(now))
    assert len(first["errors"]) == 3 and first["collected"] == []
    again = collect_due(ctx, now=Clock(now + 60))
    assert "o/r#1" in again["collected"]  # the three failing PRs are backed off, so the fourth gets its turn


def test_a_pr_that_keeps_failing_is_eventually_given_up(world):  # noqa: F811
    ctx, _p, clock, _tmp = world
    seed_review(ctx.shadow, clock, "a1", 1, at=T0)
    now = T0 + 2 * DAY
    attempts = 0

    def boom(ref, reactions=True):
        raise RuntimeError("deleted")

    _p.list_threads = boom
    for _ in range(20):
        out = collect_due(ctx, now=Clock(now))
        attempts += bool(out["errors"])
        now += 10 * DAY
    assert attempts == schedule.MAX_FAILURES
    assert schedule.due(ctx.state, ctx.shadow, now) == []
    assert [c.pr for c in schedule.due(ctx.state, ctx.shadow, now, everything=True)] == ["o/r#1"]


def test_runs_that_read_memory_are_not_evidence(world):  # noqa: F811
    ctx, _p, clock, _tmp = world
    seed_review(ctx.shadow, clock, "a1", 1, at=T0)
    rid = seed_review(ctx.shadow, clock, "b1", 2, at=T0 + 5)
    ctx.shadow.append(rid, {"type": "memory", "read": True})
    assert [r.pr for r in load_runs(ctx.shadow)] == ["o/r#1"]
    rid = "c" * 12
    ctx.shadow.append(rid, {"type": "run", "repo": "o/r", "pr": "o/r#3", "memory": {"read": True}})
    assert [r.pr for r in load_runs(ctx.shadow)] == ["o/r#1"]


def _off(tmp_path):
    _, clone = make_origin_and_clone(tmp_path)
    ctx = make_ctx(tmp_path, clone, provider=FakeProvider(),
                   memory=MemoryConfig(collect=True, store=str(tmp_path / "mem")))
    ctx.shadow = None
    ctx.config = replace(ctx.config, shadow=replace(ctx.config.shadow, store=str(tmp_path / "shadow")))
    return ctx


def test_user_invoked_collect_falls_back_to_the_configured_store(tmp_path):
    ctx = _off(tmp_path)
    clock = Clock()
    seed_review(ShadowStore(tmp_path / "shadow", now=clock), clock, "a1", 1, at=T0)
    out = collect_due(ctx, now=Clock(T0 + 2 * DAY), dry_run=True, fallback_store=True)
    assert out["due"] == ["o/r#1"]
    assert collect_due(ctx, now=Clock(T0 + 2 * DAY), dry_run=True)["due"] == []  # the automatic path honours shadow off


def test_nothing_to_collect_gives_a_hint_not_silence(tmp_path):
    ctx = _off(tmp_path)
    assert "shadow" in collect_due(ctx)["hint"]
    assert "hint" in collect_due(ctx, fallback_store=True)  # an empty store says so too
