"""A slow embedding host (seconds per text) still makes progress: batches shrink, finished ones are kept, runs converge."""

import itertools
import types
from dataclasses import dataclass

import pytest
from clihelp import make_ctx
from test_memory_semantic import DIGEST, DIM, cfg_for, seeded

from codecheck.memory import embed, semantic

pytestmark = pytest.mark.usefixtures("git_env")


@dataclass
class Clock:
    now: float = 1000.0

    def monotonic(self) -> float:
        return self.now


class SlowEmbedder:
    """Latency scales with batch size (`per_text` seconds each) on a fake clock; a call over its budget times out."""
    model = "fake-embed"

    def __init__(self, clock: Clock, per_text: float):
        self.clock, self.per_text = clock, per_text
        self.calls: list[tuple[int, float]] = []

    def digest(self):
        return DIGEST

    def embed(self, texts, budget=5.0):
        self.calls.append((len(texts), budget))
        need = self.per_text * len(texts)
        if need > budget:
            self.clock.now += budget
            return None
        self.clock.now += need
        return [[1.0] * DIM for _ in texts]


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(semantic, "time", types.SimpleNamespace(monotonic=c.monotonic))
    return c


def setup(tmp_path, monkeypatch, clock, per_text, n):
    db, _s, _ = seeded(tmp_path, n)
    emb = SlowEmbedder(clock, per_text)
    monkeypatch.setattr(semantic, "make_embedder", lambda cfg: emb)
    return db, emb, make_ctx(tmp_path, tmp_path, memory=cfg_for(tmp_path))


def test_slow_host_saves_what_finishes_instead_of_nothing(tmp_path, monkeypatch, clock):
    db, emb, ctx = setup(tmp_path, monkeypatch, clock, per_text=4.3, n=40)
    out = semantic.refresh_local(ctx, budget=120)
    assert out["embedded"] > 0
    assert out["embedded"] + out["remaining"] == 40
    assert semantic.vector_stats(db)["vectors"] == out["embedded"]
    assert out["remaining"] > 0 and "reason" in out
    assert clock.now - 1000.0 <= 120 + 1e-9  # never past the budget
    assert all(b <= 120 for _, b in emb.calls)


def test_repeated_runs_converge(tmp_path, monkeypatch, clock):
    db, _emb, ctx = setup(tmp_path, monkeypatch, clock, per_text=4.3, n=60)
    left = 60
    for _ in range(6):
        out = semantic.refresh_local(ctx, budget=120)
        assert out["remaining"] < left or left == 0
        left = out["remaining"]
        if not left:
            break
    assert left == 0 and semantic.vector_stats(db)["vectors"] == 60


def test_failed_batch_is_halved_and_retried(tmp_path, monkeypatch, clock):
    _db, emb, ctx = setup(tmp_path, monkeypatch, clock, per_text=4.3, n=20)
    semantic.refresh_local(ctx, budget=120)
    sizes = [n for n, _ in emb.calls]
    assert sizes[0] == semantic.START_BATCH < embed.BATCH
    assert any(b < a for a, b in itertools.pairwise(sizes))  # shrank after a timeout


def test_fast_host_grows_batches_and_finishes(tmp_path, monkeypatch, clock):
    _db, emb, ctx = setup(tmp_path, monkeypatch, clock, per_text=0.01, n=70)
    out = semantic.refresh_local(ctx, budget=120)
    assert out["embedded"] == 70 and out["remaining"] == 0 and "reason" not in out
    assert max(n for n, _ in emb.calls) > semantic.START_BATCH


def test_tiny_budget_reports_progress_and_reason(tmp_path, monkeypatch, clock):
    _db, _emb, ctx = setup(tmp_path, monkeypatch, clock, per_text=4.3, n=10)
    out = semantic.refresh_local(ctx, budget=10)
    assert out["embedded"] + out["remaining"] == 10
    assert out["remaining"] > 0 and out["reason"]
    assert clock.now - 1000.0 <= 10 + 1e-9


def test_service_down_stops_after_halving_to_one(tmp_path, monkeypatch, clock):
    _db, emb, ctx = setup(tmp_path, monkeypatch, clock, per_text=1000, n=5)
    out = semantic.refresh_local(ctx, budget=120)
    assert out["embedded"] == 0 and out["remaining"] == 5 and "failed" in out["reason"]
    assert len(emb.calls) <= 5
