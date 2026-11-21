import json
import os

import pytest
from clihelp import call, make_ctx
from gitutil import make_origin_and_clone

from codecheck.memory.consolidate import DAY, Params
from codecheck.memory.measure import lessons_visible_to, load_runs, measure, path_glob
from codecheck.shadow.store import ShadowStore

T0 = 1_700_000_000.0


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


def rid_of(name):
    """A valid run id (12 hex characters) from a short name like a1, zz, b1."""
    return "".join(f"{ord(c):02x}" for c in name).ljust(12, "0")[:12]


def seed_run(shadow, clock, name, pr, *, day, misses=(), findings=(), repo="o/r"):
    """One run at T0 + day. misses: (thread, path). findings: (id, path, category, outcome dict or None)."""
    rid = rid_of(name)
    clock.t = T0 + day * DAY
    shadow.append(rid, {"type": "run", "repo": repo, "pr": f"{repo}#{pr}", "tool_version": "1"})
    for fid, path, cat, outcome in findings:
        shadow.append(rid, {"type": "finding", "id": fid, "file": path, "category": cat, "message": "m", "severity": "Warning"})
        if outcome is not None:
            shadow.append(rid, {"type": "outcome", "finding_id": fid, "thread_id": f"T-{fid}", "replies": [], **outcome})
            if "verdict" in outcome:
                shadow.append(rid, {"type": "verdict", "thread_id": f"T-{fid}", "verdict": outcome["verdict"]})
    for thread, path in misses:
        shadow.append(rid, {"type": "human_comment", "thread_id": thread, "path": path, "line": 1, "author": "h", "body": "b",
                            "matches_finding": None})


@pytest.fixture
def store(tmp_path):
    clock = Clock()
    return ShadowStore(tmp_path / "shadow", now=clock), clock


def test_missing_and_empty_stores_measure_to_zero(tmp_path):
    for root in (tmp_path / "nope", tmp_path):
        out = measure(ShadowStore(root))
        assert out["store"]["runs_measured"] == 0 and out["repos"] == {} and out["summary"]["miss_recurrence_rate"] is None
    assert not (tmp_path / "nope").exists()  # measuring never creates the store


def test_a_miss_recurs_only_after_an_earlier_miss_in_the_same_bucket(store):
    shadow, clock = store
    seed_run(shadow, clock, "a1", 1, day=0, misses=[("T1", "src/app/a.py"), ("T2", "src/app/b.py")])
    seed_run(shadow, clock, "a2", 2, day=1, misses=[("T3", "src/app/c.py"), ("T4", "docs/x/y/z.md")])
    out = measure(shadow, now=lambda: T0 + 2 * DAY)
    repo = out["repos"]["o/r"]
    assert repo["missed_human_comments"] == 4
    assert repo["missed_in_bucket_with_earlier_miss"] == 1  # T3 only: T1 and T2 had nothing before them, T4 another bucket
    assert repo["missed_in_bucket_with_earlier_miss_other_pr"] == 1
    assert repo["missed_with_any_earlier_miss_in_repo"] == 2
    assert out["unweighted"] and repo["unweighted"]


def test_no_future_evidence_leaks_into_a_runs_lessons(store):
    shadow, clock = store
    seed_run(shadow, clock, "a1", 1, day=0, misses=[("T1", "src/app/a.py")])
    seed_run(shadow, clock, "a2", 2, day=1, misses=[("T2", "src/app/b.py")])
    seed_run(shadow, clock, "a3", 3, day=2, misses=[("T3", "src/app/c.py"), ("T4", "src/app/d.py")])
    runs = load_runs(shadow)
    assert [r.run_id for r in runs] == [rid_of("a1"), rid_of("a2"), rid_of("a3")]
    seen = [sum(le.support for le in lessons_visible_to(runs, i, Params()).values()) for i in range(3)]
    assert seen == [0, 1.0, 2.0]  # a run sees exactly what came before it, never its own or later comments
    assert measure(shadow, now=lambda: T0 + 3 * DAY)["repos"]["o/r"]["missed_in_bucket_with_earlier_miss"] == 3


def test_run_order_is_chronological_not_by_id(store):
    shadow, clock = store
    seed_run(shadow, clock, "zz", 1, day=0, misses=[("T1", "src/app/a.py")])  # sorts last by id, ran first
    seed_run(shadow, clock, "aa", 2, day=1, misses=[("T2", "src/app/b.py")])
    assert [r.run_id for r in load_runs(shadow)] == [rid_of("zz"), rid_of("aa")]
    assert measure(shadow, now=lambda: T0 + 2 * DAY)["repos"]["o/r"]["missed_in_bucket_with_earlier_miss"] == 1


def test_labelled_findings_with_a_prior_lesson_count_agreement(store):
    shadow, clock = store
    f = ("src/app/a.py", "test")
    seed_run(shadow, clock, "a1", 1, day=0, findings=[("f1", f[0], f[1], {"verdict": "incorrect"})])
    seed_run(shadow, clock, "a2", 2, day=1, findings=[("f2", f[0], "tests", {"verdict": "incorrect"})])  # agrees
    seed_run(shadow, clock, "a3", 3, day=2, findings=[("f3", f[0], "tests", {"verdict": "valid"})])  # disagrees
    seed_run(shadow, clock, "a4", 4, day=3, findings=[("f4", "other/dir/x.py", "tests", {"verdict": "valid"})])  # no prior
    repo = measure(shadow, now=lambda: T0 + 4 * DAY)["repos"]["o/r"]
    assert repo["disputed_findings"] == {"total": 2, "with_prior_lesson": 1, "prior_agreed": 1}
    # a3 sees two disputes and confirms nothing: the prior lesson disagrees with the later confirmation
    assert repo["confirmed_findings"] == {"total": 2, "with_prior_lesson": 1, "prior_disagreed": 1}


def test_buckets_reaching_the_activation_threshold_are_counted_per_repo(store):
    shadow, clock = store
    seed_run(shadow, clock, "a1", 1, day=0, misses=[("T1", "src/app/a.py")])
    seed_run(shadow, clock, "a2", 2, day=1, misses=[("T2", "src/app/b.py")])
    seed_run(shadow, clock, "b1", 1, day=2, repo="x/y", misses=[("T9", "src/app/a.py")])
    out = measure(shadow, now=lambda: T0 + 3 * DAY)
    assert out["repos"]["o/r"]["buckets"]["active"] == 1
    assert "active" not in out["repos"]["x/y"]["buckets"]  # repos never mix
    assert out["summary"]["active_buckets"] == 1


def test_measure_writes_nothing(store):
    shadow, clock = store
    seed_run(shadow, clock, "a1", 1, day=0, misses=[("T1", "src/app/a.py")])
    before = sorted((p.name, p.stat().st_mtime_ns, p.read_bytes()) for p in shadow.root.iterdir())
    measure(shadow)
    assert sorted((p.name, p.stat().st_mtime_ns, p.read_bytes()) for p in shadow.root.iterdir()) == before


def test_path_glob():
    assert path_glob("src/app/deep/x.py") == "src/app/**"
    assert path_glob("src/x.py") == "src/**"
    assert path_glob("x.py") == "*"
    assert path_glob("../../etc/-evil/x.py") == "etc/-evil/**"
    assert path_glob(None) == "*"


def test_command_prints_one_json_document_and_works_without_a_store(tmp_path):
    _, clone = make_origin_and_clone(tmp_path)
    ctx = make_ctx(tmp_path, clone)
    code, out = call(ctx, "memory", "measure")
    assert code == 0 and out["store"]["runs_measured"] == 0
    seed_clock = Clock()
    shadow = ShadowStore(tmp_path / "shadow", now=seed_clock)
    seed_run(shadow, seed_clock, "a1", 1, day=0, misses=[("T1", "src/app/a.py")])
    code, out = call(ctx, "memory", "measure")
    assert code == 0 and json.dumps(out) and out["store"]["human_comments"] == 1
    assert os.listdir(tmp_path / "shadow") == [rid_of("a1") + ".jsonl"]
