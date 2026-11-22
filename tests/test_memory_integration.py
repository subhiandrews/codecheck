"""Phases 2 to 4 together: `context` refreshes the replica from the memory server first, collection pushes after it."""

import time
from dataclasses import replace
from pathlib import Path

import pytest
from test_memory_collect import T0, Clock, seed_review, thread, world  # noqa: F401 - `world` is a fixture
from test_memory_read import (
    blind_pr,
    context,
    lesson,
    local_ctx,
    memory_cfg,
    store,
    sub,
)
from test_memory_server import Srv, item_for, make_ev

from codecheck.memory import sync
from codecheck.memory.autocollect import collect_due
from codecheck.memory.consolidate import DAY
from codecheck.models import PRRef

pytestmark = pytest.mark.usefixtures("git_env")

URL = "http://127.0.0.1:9"


class Calls:
    def __init__(self, result=None, exc=None, delay=0.0):
        self.n, self.result, self.exc, self.delay = 0, result or {"pulled": True}, exc, delay

    def __call__(self, ctx, *a, **k):
        self.n += 1
        if self.delay:
            time.sleep(self.delay)
        if self.exc:
            raise self.exc
        return self.result


@pytest.fixture
def pulls(monkeypatch):
    c = Calls()
    monkeypatch.setattr(sync, "pull_if_stale", c)
    return c


def test_a_configured_server_is_pulled_once_before_the_reviewers_read(tmp_path, pulls):
    ctx = local_ctx(tmp_path, memory=memory_cfg(tmp_path, url=URL, share_repos=("o/r",)))
    _out, meta = context(ctx)
    assert pulls.n == 1
    assert meta["memory"]["pull"] == {"pulled": True}


@pytest.mark.parametrize("over", [{}, {"enabled": False}, {"read": False}])
def test_nothing_is_pulled_without_a_url_or_when_memory_is_off(tmp_path, pulls, over):
    cfg = memory_cfg(tmp_path, url=URL, **over) if over else memory_cfg(tmp_path)  # {} case: url empty
    ctx = local_ctx(tmp_path, memory=cfg)
    _out, meta = context(ctx)
    assert pulls.n == 0 and "pull" not in meta["memory"]


def test_an_aggregator_only_configuration_still_pulls_once_and_writes_no_reviewer_file(tmp_path, pulls):
    ctx = local_ctx(tmp_path, memory=memory_cfg(tmp_path, url=URL, share_repos=("o/r",), reviewers=False))
    out, meta = context(ctx)
    assert pulls.n == 1
    assert meta["memory"] == {"read": False, "reason": "reviewers off", "pull": {"pulled": True}}
    assert not (Path(out["run_dir"]) / "memory.md").exists()


@pytest.mark.parametrize("calls", [Calls(exc=RuntimeError("boom")), Calls(exc=KeyboardInterrupt()), Calls(delay=5.0)])
def test_an_aggregator_only_pull_is_guarded_and_bounded(tmp_path, monkeypatch, calls):
    monkeypatch.setattr(sync, "pull_if_stale", calls)
    ctx = local_ctx(tmp_path, memory=memory_cfg(tmp_path, url=URL, share_repos=("o/r",), reviewers=False, timeout_seconds=2))
    t = time.monotonic()
    _out, meta = context(ctx)
    assert time.monotonic() - t < 4.0 and calls.n == 1
    assert meta["memory"]["reason"] == "reviewers off"


def test_a_blind_run_with_reviewers_off_pulls_nothing(tmp_path, pulls):
    from clihelp import run_pr_context

    ctx, _provider, pr = blind_pr(tmp_path)
    ctx.config = replace(ctx.config, memory=memory_cfg(tmp_path, url=URL, share_repos=("o/r",), reviewers=False))
    out = run_pr_context(ctx, pr)
    assert out["memory"] == {"read": False, "reason": "blind"}
    assert pulls.n == 0


def test_a_blind_run_pulls_nothing(tmp_path, pulls):
    from clihelp import run_pr_context

    ctx, _provider, pr = blind_pr(tmp_path)
    ctx.config = replace(ctx.config, memory=memory_cfg(tmp_path, url=URL, share_repos=("o/r",)))
    out = run_pr_context(ctx, pr)
    assert out["memory"] == {"read": False, "reason": "blind"}
    assert pulls.n == 0


@pytest.mark.parametrize("calls", [Calls(exc=RuntimeError("boom")), Calls(exc=KeyboardInterrupt()), Calls(delay=5.0)])
def test_a_failing_or_hanging_server_never_fails_or_delays_context(tmp_path, monkeypatch, calls):
    store(tmp_path, lesson(statement="Local replica lesson."))
    monkeypatch.setattr(sync, "pull_if_stale", calls)
    ctx = local_ctx(tmp_path, memory=memory_cfg(tmp_path, url=URL, share_repos=("o/r",), timeout_seconds=2))
    t = time.monotonic()
    out, meta = context(ctx)
    assert time.monotonic() - t < 4.0
    assert calls.n == 1
    if calls.delay:
        # a slow pull may eat the shared budget and cost the read, never the context
        assert meta["memory"]["reason"] in ("ok", "timeout")
    else:
        # a failing pull never costs the replica read
        assert meta["memory"]["reason"] == "ok"
    if meta["memory"]["reason"] == "ok":
        assert "Local replica lesson." in (Path(out["run_dir"]) / "memory.md").read_text()


def test_a_teammates_lesson_is_pulled_over_http_and_reaches_memory_md(tmp_path):
    srv = Srv(sub(tmp_path, "s"))
    srv.service.now = time.time  # real time: retrieval drops lessons that are old by the wall clock
    try:
        evs = [make_ev(f"T{i}", signal="human_miss", pr=f"o/r#{i}", ts=time.time() - 3 * DAY + i) for i in range(1, 4)]
        status, body = srv.req("PUT", "/v1/lessons", {"items": [item_for(evs, "lessons")]}, srv.alice)
        assert status == 200, body
        tf = tmp_path / "token"
        tf.write_text(srv.bob + "\n")
        tf.chmod(0o600)
        cfg = memory_cfg(tmp_path, url=f"http://127.0.0.1:{srv.port}", token_file=str(tf), share_repos=("o/r",),
                         timeout_seconds=5)
        ctx = local_ctx(sub(tmp_path, "w"), memory=cfg)
        out, meta = context(ctx)
        assert meta["memory"]["pull"]["pulled"] is True, meta["memory"]
        # teammate evidence arrives at level `lessons` with no quote, so memory.md shows roles and counts only
        assert meta["memory"]["read"] is True, meta["memory"]
        text = (Path(out["run_dir"]) / "memory.md").read_text()
        assert f"## Lesson 1: {evs[0].lesson_id}" in text and "maintainer x3" in text
    finally:
        srv.close()


# ---- the push side of collection ----


def _world_with_pr(world):  # noqa: F811
    ctx, provider, clock, _tmp = world
    seed_review(ctx.shadow, clock, "a1", 1, at=T0)
    provider.add_pr(1)
    provider.threads[PRRef("o/r", 1)] = [thread("T1")]
    return ctx


@pytest.mark.parametrize("over", [{}, {"url": URL}, {"share_repos": ("o/r",)}])
def test_collection_pushes_only_with_a_url_and_share_repos(world, monkeypatch, over):  # noqa: F811
    sent = []
    monkeypatch.setattr(sync, "sync", lambda ctx, **k: sent.append(k) or {"ok": True})
    ctx = _world_with_pr(world)
    ctx.config = replace(ctx.config, memory=replace(ctx.config.memory, **over))
    out = collect_due(ctx, now=Clock(T0 + 2 * DAY))
    assert out["consolidated"] and "pushed" not in out and sent == []  # default: the result is what it was before


def test_collection_pushes_after_consolidating_and_a_failing_server_is_only_reported(world, monkeypatch):  # noqa: F811
    sent = []
    monkeypatch.setattr(sync, "sync", lambda ctx, **k: sent.append(k) or {"ok": True})
    ctx = _world_with_pr(world)
    ctx.config = replace(ctx.config, memory=replace(ctx.config.memory, url=URL, share_repos=("o/r",)))
    out = collect_due(ctx, now=Clock(T0 + 2 * DAY))
    assert out["pushed"] == {"ok": True}
    assert len(sent) == 1 and sent[0]["do_pull"] is False and sent[0]["http_timeout"] > 0 and not sent[0]["cancel"].is_set()

    def boom(ctx, **k):
        raise RuntimeError("server down")

    monkeypatch.setattr(sync, "sync", boom)
    out = collect_due(ctx, now=Clock(T0 + 2 * DAY), everything=True)
    assert out["pushed"] == {"ok": False, "error": "RuntimeError"} and out["errors"] == []
