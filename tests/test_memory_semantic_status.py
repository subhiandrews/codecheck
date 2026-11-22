"""`semantic` in the memory result and in `memory status`: why similarity search gave nothing, never raw error text."""

import time

import pytest
from clihelp import call
from test_memory_read import local_ctx
from test_memory_semantic import FakeEmbedder, cfg_for, seeded

from codecheck.config import MemoryConfig
from codecheck.memory import inject, semantic
from codecheck.memory.semantic import Query


def build(ctx, tmp_path):
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    return inject.safe_build(ctx, run, stage=inject.REVIEWERS, repo="o/r", base_ref="main", blind=False,
                             paths=["src/app/b.py"], queries=[Query("user input is not checked for null before strip")])


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    emb = FakeEmbedder()
    monkeypatch.setattr(semantic, "make_embedder", lambda cfg: emb if cfg.embed_model else None)
    seeded(tmp_path)
    c = local_ctx(tmp_path, memory=cfg_for(tmp_path))
    semantic.refresh_local(c)
    c.emb = emb
    return c


def test_a_working_lookup_says_ok(ctx, tmp_path):
    out = build(ctx, tmp_path)
    assert out["semantic"] == "ok", out


def test_an_unreachable_embedder_says_so_and_still_returns_exact_key_behaviour(ctx, tmp_path):
    ctx.emb.fail = True
    out = build(ctx, tmp_path)
    assert out["semantic"] == "fallback: unreachable" and out["read"] is False


def test_a_pin_mismatch_says_model_changed(ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(semantic, "pin_mismatch", lambda c, e: True)
    assert build(ctx, tmp_path)["semantic"] == "fallback: model changed"


def test_a_slow_embedder_says_timeout(tmp_path, monkeypatch):
    seeded(tmp_path)
    c = local_ctx(tmp_path, memory=cfg_for(tmp_path, timeout_seconds=0.4))
    monkeypatch.setattr(semantic, "client_ranked", lambda *a, **k: time.sleep(2) or [])
    assert build(c, tmp_path)["semantic"] == "fallback: timeout"


def test_a_raising_lookup_says_error_without_its_text(ctx, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret detail from the server")

    monkeypatch.setattr(semantic, "client_ranked", boom)
    out = build(ctx, tmp_path)
    assert out["semantic"] == "fallback: error" and "secret" not in str(out)


def test_the_server_mode_refusal_says_refused(tmp_path, monkeypatch):
    seeded(tmp_path)
    c = local_ctx(tmp_path, memory=cfg_for(tmp_path, embed_at="server"))
    monkeypatch.setattr(semantic, "server_ranked", lambda *a, **k: a[-1].update(reason="refused") or [])
    assert build(c, tmp_path)["semantic"] == "fallback: refused"


def test_no_field_when_embeddings_are_off(tmp_path):
    seeded(tmp_path)
    c = local_ctx(tmp_path, memory=MemoryConfig(enabled=True, read=True, reviewers=True, store=str(tmp_path / "mem")))
    out = build(c, tmp_path)
    assert "semantic" not in out


def test_memory_status_shows_the_check(ctx):
    code, st = call(ctx, "memory", "status")
    assert code == 0 and st["embedding"]["semantic"] == "ok"
    ctx.emb.fail = True
    code, st = call(ctx, "memory", "status")
    assert st["embedding"]["semantic"] == "fallback: unreachable"
    code, st = call(ctx, "memory", "status", "--offline")
    assert "semantic" not in st["embedding"]
