"""PR #74 review claims: the pull-failure stamp without the lock, a cheap emptiness check, queries inside the guard,
and COLLABORATOR no longer meaning maintainer."""

import threading
import time

import pytest
from clihelp import make_ctx
from gitutil import make_origin_and_clone
from test_memory_collect import Clock, seed_review
from test_memory_read import context, local_ctx, memory_cfg
from test_memory_sync import Env

from codecheck.config import MemoryConfig
from codecheck.memory import inject, semantic, sync
from codecheck.memory.autocollect import collect_due
from codecheck.shadow.roles import derive_role
from codecheck.shadow.store import ShadowStore

pytestmark = pytest.mark.usefixtures("git_env")


# 1. the failure stamp must not need sync.json.lock, which the stuck worker holds

def test_an_abandoned_pull_is_stamped_while_the_worker_holds_the_lock(tmp_path, monkeypatch):
    _, clone = make_origin_and_clone(tmp_path)
    env = Env(tmp_path, "a", clone, url="http://127.0.0.1:1", token="cck_x", timeout_seconds=1)
    release = threading.Event()
    entered = threading.Event()
    pulls = []

    def hung_pull(ctx, transport, repos, state, **kw):  # the real pull_if_stale runs and holds the real lock
        pulls.append(1)
        entered.set()
        release.wait(10)
        return {"pulled": False, "error": "TimeoutError"}

    monkeypatch.setattr(sync, "pull", hung_pull)
    try:
        started = time.monotonic()
        out = inject._pull_replica(env.ctx, inject.REVIEWERS, 1.0, "o/r")
        took = time.monotonic() - started
        assert entered.is_set() and out == {"pulled": False, "reason": "timeout"}
        assert took < 1.5, took  # budget/2 plus no wait on the lock (it used to add LOCK_WAIT_SECONDS)
        assert abs(time.time() - sync._failed_stamp(env.ctx)) < 5  # stamped even though the lock is held
        again = sync.pull_if_stale(env.ctx, repos=["o/r"])
        assert again == {"pulled": False, "reason": "a recent pull failed"} and len(pulls) == 1
    finally:
        release.set()


def test_the_failure_stamp_expires_and_a_bad_file_is_ignored(tmp_path, monkeypatch):
    _, clone = make_origin_and_clone(tmp_path)
    env = Env(tmp_path, "a", clone, url="http://127.0.0.1:1", token="cck_x")
    assert sync._failed_stamp(env.ctx) == 0.0
    (env.root / "mem").mkdir(exist_ok=True)
    (env.root / "mem" / sync.PULL_FAILED_FILE).write_text("garbage")
    assert sync._failed_stamp(env.ctx) == 0.0
    sync._write_failed_stamp(env.ctx, 1000.0)
    monkeypatch.setattr(sync, "clock", lambda: 1000.0 + sync.RETRY_AFTER_FAILED_PULL + 1)
    assert sync.pull_if_stale(env.ctx, repos=["o/r"]).get("reason") !="a recent pull failed"


# 2. emptiness is a glob, not a read of every run file

def test_autocollect_does_not_read_every_run_to_test_for_emptiness(tmp_path, monkeypatch):
    clock = Clock()
    ctx = make_ctx(tmp_path, make_origin_and_clone(tmp_path)[1], memory=MemoryConfig(collect=True, store=str(tmp_path / "mem")))
    ctx.shadow = ShadowStore(tmp_path / "shadow", now=clock)
    empty = collect_due(ctx, dry_run=True)
    assert "holds no reviews yet" in empty["hint"]
    seed_review(ctx.shadow, clock, "a1", 1, at=1_700_000_000.0)
    calls = []
    real = ShadowStore.ordered_run_ids
    monkeypatch.setattr(ShadowStore, "ordered_run_ids", lambda self: calls.append(1) or real(self))
    out = collect_due(ctx, dry_run=True)
    assert "hint" not in out
    assert len(calls) == 1  # due() only; the emptiness check no longer sorts (and so reads) every run


# 3. the semantic queries are computed inside safe_build's guard

def test_a_raising_diff_queries_leaves_context_normal(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("semantic import exploded")

    monkeypatch.setattr(semantic, "diff_queries", boom)
    ctx = local_ctx(tmp_path, memory=memory_cfg(tmp_path, embed_model="some-model"))
    out, meta = context(ctx)
    assert out["files"] == 1
    assert meta["memory"]["read"] is False and meta["memory"]["reason"] == "error"
    assert "semantic import exploded" in meta["memory"]["error"]


def test_diff_queries_is_never_called_with_memory_disabled(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(semantic, "diff_queries", lambda *a, **k: calls.append(1) or [])
    ctx = local_ctx(tmp_path, memory=MemoryConfig(embed_model="some-model", store=str(tmp_path / "mem")))
    out, meta = context(ctx)
    assert out["files"] == 1 and meta["memory"]["read"] is False and not calls


# 4. COLLABORATOR may hold read or triage access only

@pytest.mark.parametrize("assoc,role", [("OWNER", "maintainer"), ("COLLABORATOR", "contributor"), ("collaborator", "contributor"),
                                        ("MEMBER", "unknown")])
def test_association_fallback_only_trusts_owner(assoc, role):
    assert derive_role("maya", me="me", pr_author="pat", can_write=None, association=assoc) == role
    assert derive_role("maya", me="me", pr_author="pat", can_write=True, association=assoc) == "maintainer"
