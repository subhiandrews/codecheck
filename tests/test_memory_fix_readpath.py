"""Read-path fixes: the semantic stage's budget, the pull timeout backoff, one loopback rule, lazy imports."""

import subprocess
import sys
import time
from dataclasses import replace

import pytest
from test_memory_read import local_ctx
from test_memory_semantic import cfg_for, seeded

from codecheck.config import ConfigError, MemoryConfig, validate_memory
from codecheck.memory import embed, inject, semantic, sync
from codecheck.memory.semantic import Query


def build(ctx, tmp_path):
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    return inject.safe_build(ctx, run, stage=inject.REVIEWERS, repo="o/r", base_ref="main", blind=False,
                             paths=["src/app/b.py"], queries=[Query("user input is not checked for null before strip")]), run


# 1. the semantic stage gets what is left of the budget and falls back to the exact-key result

def test_semantic_budget_is_the_time_left_not_half_the_timeout(tmp_path, monkeypatch):
    seeded(tmp_path)
    ctx = local_ctx(tmp_path, memory=cfg_for(tmp_path, timeout_seconds=10))
    seen = {}

    def fake(c, db, r, q, k, budget, why=None):
        seen["b"] = budget
        return []

    monkeypatch.setattr(semantic, "client_ranked", fake)
    monkeypatch.setattr(embed, "BUDGET_SECONDS", 100.0)  # so only the remaining time limits it
    semantic.ranked_for(ctx, None, "o/r", [Query("x")], "miss", deadline=time.monotonic() + 8.0)
    assert 6.0 < seen["b"] <= 8.0  # the old rule would give 5.0


def test_a_semantic_overrun_keeps_the_exact_key_lessons(tmp_path, monkeypatch):
    db, _s, _k = seeded(tmp_path)
    ctx = local_ctx(tmp_path, memory=cfg_for(tmp_path, timeout_seconds=1))
    db.recompute()
    exact = db.lessons()
    assert exact
    monkeypatch.setattr(inject, "miss_lessons", lambda *a, **k: exact)
    monkeypatch.setattr(semantic, "ranked_for", lambda *a, **k: time.sleep(5) or [])
    out, run = build(ctx, tmp_path)
    assert out["read"] is True and out["ids"] == [le.id for le in exact]
    assert out["semantic"] == "fallback: timeout"
    assert (run / "memory.md").exists()


# 2. a pull that timed out stamps the failure so the 60 s backoff applies

def test_a_timed_out_pull_stamps_the_failure(tmp_path, monkeypatch):
    ctx = local_ctx(tmp_path, memory=cfg_for(tmp_path, url="http://127.0.0.1:1", share_repos=("o/r",)))
    monkeypatch.setattr(sync, "pull_if_stale", lambda *a, **k: time.sleep(5) or {"pulled": True})
    out = inject._pull_replica(ctx, inject.REVIEWERS, 0.2, "o/r")
    assert out == {"pulled": False, "reason": "timeout"}
    stamp = float(sync._fresh_state(ctx).get("last_pull_failed_ts", 0.0))
    assert abs(time.time() - stamp) < 5


# 3. one loopback rule

@pytest.mark.parametrize("url", ["http://127.evil.com", "http://127.0.0.1.nip.io", "http://127.1.2.3.example.org:8787"])
def test_config_rejects_fake_loopback_hosts(url):
    with pytest.raises(ConfigError):
        validate_memory(replace(MemoryConfig(), url=url))


@pytest.mark.parametrize("url", ["http://localhost:1", "http://127.0.0.1:1", "http://[::1]:1", "http://127.5.5.5:1"])
def test_real_loopback_is_accepted(url):
    validate_memory(replace(MemoryConfig(), url=url))


def test_runtime_and_config_share_one_function():
    from codecheck import config
    from codecheck.memory import server

    assert server.is_loopback is config.is_loopback_host


# 4. lazy imports and cheaper helpers

def test_importing_the_cli_loads_no_http_server_or_ssl():
    code = ("import sys; sys.path[:0] = ['src']; import codecheck.cli; "
            "bad = [m for m in ('http.server', 'ssl') if m in sys.modules]; print(bad); sys.exit(1 if bad else 0)")
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stdout + r.stderr


def test_dot_checks_length_and_multiplies():
    assert abs(embed.dot([0.6, 0.8], [0.6, 0.8]) - 1.0) < 1e-9
    assert embed.dot([1.0], [1.0, 2.0]) == 0.0


def test_digest_is_fetched_once(monkeypatch):
    e = embed.OllamaEmbedder("http://127.0.0.1:1", "m")
    calls = []

    def fake(*a, **k):
        calls.append(a)
        return {"models": [{"name": "m:latest", "digest": "abc"}]}

    monkeypatch.setattr(e, "_call", fake)
    assert e.digest() == "abc" and e.digest() == "abc"
    assert len(calls) == 1
