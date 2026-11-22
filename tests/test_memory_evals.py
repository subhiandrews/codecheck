"""The memory evals: retrieval scoring (`eval --memory-retrieval`), the paired effect eval (`eval --memory-effect`) and
`memory report`. None of these tests needs a model or a network."""

import json
from pathlib import Path

import pytest
from clihelp import call, make_ctx
from fakes import FakeRunner

from codecheck.errors import CodeCheckError
from codecheck.evals import memory_effect, memory_retrieval
from codecheck.evals.live import DEFAULT_SNIPPETS, load_snippets
from codecheck.evals.memory_retrieval import (
    DEFAULT_CASES,
    RETRIEVERS,
    case_problems,
    evaluate,
    load_cases,
    register_retriever,
    run_retrieval,
)
from codecheck.evals.snippet import snippet_problems
from codecheck.memory import report as memory_report
from codecheck.shadow.store import ShadowStore

PROBE = ("claude", "-p", "reply with ok", "--output-format", "json", "--permission-mode", "dontAsk")

CASE = {
    "id": "tiny", "description": "d", "files_at_base": ["src/a/f.py", "src/b/f.py"],
    "lessons": [
        {"name": "la", "kind": "miss", "path_glob": "src/a/**", "language": "python", "statement": "about a"},
        {"name": "lb", "kind": "miss", "path_glob": "src/b/**", "language": "python", "statement": "about b"},
    ],
    "queries": [{"name": "q", "mode": "diff", "paths": ["src/a/f.py"], "expect": ["la"], "expect_absent": ["lb"]}],
}


def _ids(case):
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as d:
        return memory_retrieval.seed(case, Path(d))[1]


def _write(directory: Path, case: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{case['id']}.json").write_text(json.dumps(case))


# --- the shipped cases -------------------------------------------------------------------------------------------


def test_shipped_cases_validate_and_cover_the_required_situations():
    assert DEFAULT_CASES == Path(__file__).resolve().parent / "memory" / "retrieval"
    cases = load_cases(DEFAULT_CASES)
    assert len(cases) >= 12
    for c in cases:
        assert case_problems(c) == [], c["id"]
    ids = {c["id"] for c in cases}
    assert {"same-area-different-wording", "cross-language-near-miss", "expired-lesson-not-shown", "candidate-lesson-not-shown",
            "contested-shown-with-both-counts", "path-no-longer-exists", "poisoned-instruction-lesson",
            "category-alias-variants"} <= ids


def test_exact_baseline_passes_every_exact_tier_query_and_misses_the_semantic_ones():
    out = run_retrieval(DEFAULT_CASES)
    exact = out["retrievers"]["exact"]
    assert out["advisory"] is True and "passed" not in out
    assert exact["by_tier"]["exact"]["recall_at_5"] == 1.0 and exact["by_tier"]["exact"]["mrr"] == 1.0
    assert exact["by_tier"]["semantic"]["recall_at_5"] == 0.0  # the headroom a semantic retriever is for
    assert exact["absent_violations"] == 0 and exact["render_failures"] == 0
    assert 0 < exact["recall_at_5"] < 1 and 0 < exact["mrr"] < 1
    assert {p["query"] for p in exact["problems"]} == {"sibling-directory-same-topic", "free-wording-for-validation"}


def test_case_files_are_validated():
    assert case_problems(CASE) == []
    assert case_problems([]) == ["expected a JSON object"]
    bad = json.loads(json.dumps(CASE))
    bad["queries"][0]["expect"] = ["nope"]
    bad["lessons"].append({**bad["lessons"][0], "name": "twin"})  # same scope and kind: same id
    bad["lessons"][1]["path_glob"] = "../x/**"
    problems = " ".join(case_problems(bad))
    assert "unknown lessons: nope" in problems and "share an id" in problems and "path_glob" in problems
    assert case_problems({**CASE, "queries": [{"name": "q", "mode": "diff", "paths": ["a.py"]}]})  # needs expect or expect_absent


def test_load_cases_names_the_bad_file(tmp_path):
    (tmp_path / "broken.json").write_text("{nope")
    with pytest.raises(CodeCheckError, match="broken.json"):
        load_cases(tmp_path)
    _write(tmp_path / "ok", {**CASE, "queries": []})
    with pytest.raises(CodeCheckError, match="tiny.json"):
        load_cases(tmp_path / "ok")
    assert load_cases(tmp_path / "missing") == []


# --- the harness math --------------------------------------------------------------------------------------------


def _rank_of(order):
    """A retriever returning the case's lessons in the given name order (ids resolved from the seeded names)."""
    ids = _ids(CASE)
    return lambda q: [ids[n] for n in order]


def test_recall_and_mrr_on_fake_retrievers():
    case = json.loads(json.dumps(CASE))
    case["queries"] = [{"name": "q", "mode": "diff", "paths": ["src/a/f.py"], "expect": ["la", "lb"]}]
    [row] = evaluate(case, _rank_of(["lb", "la"]))
    assert row["recall_at_5"] == 1.0 and row["rr"] == 1.0
    [row] = evaluate(case, _rank_of([]))
    assert row["recall_at_5"] == 0.0 and row["rr"] == 0.0 and row["missing"] == ["la", "lb"]
    case["queries"][0]["expect"] = ["lb"]
    [row] = evaluate(case, _rank_of(["la", "lb"]))
    assert row["rr"] == 0.5


def test_only_the_top_five_count_and_rank_is_reciprocal():
    names = [f"l{i}" for i in range(7)]
    case = {"id": "many", "description": "d", "files_at_base": ["d/f.py"],
            "lessons": [{"name": n, "kind": "miss", "path_glob": f"d{i}/**", "statement": "s"} for i, n in enumerate(names)],
            "queries": [{"name": "q", "mode": "diff", "paths": ["d/f.py"], "expect": ["l5", "l6"]}]}
    ids = _ids(case)
    [row] = evaluate(case, lambda q: [ids[n] for n in names])
    assert row["recall_at_5"] == 0.0  # ranks 6 and 7 are outside the top 5
    [row] = evaluate(case, lambda q: [ids[n] for n in ["l6", "l0", "l1", "l2", "l5"]])
    assert row["recall_at_5"] == 1.0 and row["rr"] == 1.0
    [row] = evaluate(case, lambda q: [ids[n] for n in ["l0", "l1", "l2", "l5"]])
    assert row["rr"] == pytest.approx(0.25) and row["recall_at_5"] == 0.5


def test_absent_lessons_are_counted_as_violations():
    [row] = evaluate(CASE, _rank_of(["la", "lb"]))
    assert row["unexpected"] == ["lb"]


def test_a_dropped_or_unframed_poisoned_lesson_is_a_render_failure():
    case = json.loads((DEFAULT_CASES / "poisoned-instruction-lesson.json").read_text())
    ids = _ids(case)
    rows = evaluate(case, lambda q: [])  # dropped silently: also a recall failure
    assert rows[0]["recall_at_5"] == 0.0 and rows[0]["render_failures"]
    rows = evaluate(case, lambda q: [ids["poison-miss"]] if q.mode == "diff" else [ids["poison-dispute"]])
    assert all(r["render_failures"] == [] and r["recall_at_5"] == 1.0 for r in rows)


def test_aggregate_scores_are_means_over_scored_queries():
    out = run_retrieval(DEFAULT_CASES)["retrievers"]["exact"]
    rows = [r for c in out["per_case"].values() for r in c["results"]]
    scored = [r for r in rows if "recall_at_5" in r]
    assert out["scored"] == len(scored) < out["queries"] == len(rows)
    assert out["recall_at_5"] == pytest.approx(sum(r["recall_at_5"] for r in scored) / len(scored), abs=1e-3)
    assert out["mrr"] == pytest.approx(sum(r["rr"] for r in scored) / len(scored), abs=1e-3)


# --- the extension point -----------------------------------------------------------------------------------------


def test_a_registered_retriever_is_scored_next_to_the_baseline(tmp_path):
    _write(tmp_path, CASE)
    ids = _ids(CASE)
    reg = dict(RETRIEVERS)
    register_retriever("fake-semantic", lambda q: [ids["la"]])
    try:
        out = run_retrieval(tmp_path)
        assert set(out["retrievers"]) == {"exact", "fake-semantic"}
        assert out["retrievers"]["fake-semantic"]["recall_at_5"] == 1.0
        assert run_retrieval(tmp_path, only="fake-semantic")["retrievers"].keys() == {"fake-semantic"}
    finally:
        RETRIEVERS.clear()
        RETRIEVERS.update(reg)
    assert "fake-semantic" not in RETRIEVERS
    with pytest.raises(CodeCheckError, match="unknown retriever"):
        run_retrieval(tmp_path, only="nope")


def test_unavailable_retriever_skips_with_if_reachable_and_errors_without(tmp_path):
    _write(tmp_path, CASE)
    reg = {**RETRIEVERS, "needs-ollama": (lambda q: pytest.fail("must not run"), lambda: "Ollama is not reachable")}
    out = run_retrieval(tmp_path, if_reachable=True, retrievers=reg)
    assert out["retrievers"]["needs-ollama"] == {"skipped": "not available: Ollama is not reachable"}
    assert out["retrievers"]["exact"]["recall_at_5"] == 1.0  # the others still ran
    with pytest.raises(CodeCheckError, match="Ollama is not reachable"):
        run_retrieval(tmp_path, retrievers=reg)


def test_cli_retrieval_flags(tmp_path):
    ctx = make_ctx(tmp_path, tmp_path)
    code, out = call(ctx, "eval", "--memory-retrieval", "--retriever", "exact")
    assert code == 0 and out["retrievers"]["exact"]["queries"] > 12 and out["advisory"] is True
    code, out = call(ctx, "eval", "--memory-retrieval", "--only", "x")
    assert code == 1 and "--memory-retrieval takes only" in out["error"]
    code, out = call(ctx, "eval", "--live", "--retriever", "exact")
    assert code == 1 and "--memory-retrieval only" in out["error"]
    code, out = call(ctx, "eval", "--memory-effect", "--cases", str(tmp_path))
    assert code == 1 and "--memory-effect takes only" in out["error"]
    code, out = call(ctx, "eval", "--memory-retrieval", "--cases", str(tmp_path / "nope"))
    assert code == 1 and "no such cases directory" in out["error"]


# --- the effect eval ---------------------------------------------------------------------------------------------


def test_shipped_effect_snippets_validate_and_carry_lessons():
    snippets = {s["name"]: s for s in load_snippets(DEFAULT_SNIPPETS) if s.get("memory")}
    assert {"poisoned-memory-lesson", "memory-dispute-lesson-downgrades-false-positive", "memory-miss-lesson-surfaces-issue"} <= set(snippets)
    for s in snippets.values():
        assert s["calibrated"] is False and snippet_problems(s) == []
    dispute = snippets["memory-dispute-lesson-downgrades-false-positive"]
    rule = dispute["must_not_report"][0]
    assert "time.sleep" in dispute["change"][rule["file"]].splitlines()[rule["line"] - 1]
    real = dispute["expected"][0]
    assert "range(attempts - 1)" in dispute["change"][real["file"]].splitlines()[real["line"] - 1]
    miss = snippets["memory-miss-lesson-surfaces-issue"]
    assert "datetime.now()" in miss["change"][miss["expected"][0]["file"]].splitlines()[miss["expected"][0]["line"] - 1]
    assert [le["kind"] for le in miss["memory"]["lessons"]] == ["miss"]
    assert {le["kind"] for le in dispute["memory"]["lessons"]} == {"finding"}


def test_memory_effect_is_validated():
    base = load_snippets(DEFAULT_SNIPPETS)
    s = next(x for x in base if x["name"] == "memory-dispute-lesson-downgrades-false-positive")
    assert snippet_problems({**s, "memory_effect": {"off": {"expected": []}}}) == []
    assert any("memory_effect" in p for p in snippet_problems({**s, "memory_effect": {"on": {}}}))
    assert any("memory_effect.off" in p for p in snippet_problems({**s, "memory_effect": {"off": {"expected": [{"file": "x"}]}}}))
    plain = next(x for x in base if x["name"] == "off-by-one")
    assert any("needs memory" in p for p in snippet_problems({**plain, "memory_effect": {"off": {}}}))


def test_off_arm_drops_the_lessons_and_takes_its_own_expectations():
    s = next(x for x in load_snippets(DEFAULT_SNIPPETS) if x["name"] == "memory-dispute-lesson-downgrades-false-positive")
    off = memory_effect.off_arm(s)
    assert "memory" not in off and "memory_effect" not in off and off["must_not_report"] == [] and off["expected"] == s["expected"]
    poisoned = next(x for x in load_snippets(DEFAULT_SNIPPETS) if x["name"] == "poisoned-memory-lesson")
    assert memory_effect.off_arm(poisoned)["expected"] == poisoned["expected"]


def test_compare_names_suppressed_surfaced_and_lost_findings():
    s = {"expected": [{"file": "a.py", "line": 5, "category": ["bug"], "severity": "critical"}],
         "must_not_report": [{"file": "a.py", "line": 9, "severity": ["warning", "critical"]}]}
    crit = {"file": "a.py", "line": 5, "category": "bug", "severity": "critical"}
    fp = {"file": "a.py", "line": 9, "category": "perf", "severity": "warning"}
    assert memory_effect.compare(s, [fp], [crit])["suppressed"] == s["must_not_report"]
    assert memory_effect.compare(s, [], [crit])["surfaced"] == s["expected"]
    assert memory_effect.compare(s, [crit], [])["lost"] == s["expected"]  # what a poisoned lesson would do
    assert memory_effect.compare(s, [fp], [fp, crit])["still_reported"] == s["must_not_report"]
    same = memory_effect.compare(s, [crit], [crit])
    assert same["suppressed"] == same["surfaced"] == same["lost"] == []


def _fake_run(off_got, on_got):
    def run(ctx, snippet, workdir):
        got = on_got if snippet.get("memory") else off_got
        from codecheck.evals.match import match_case

        return {"name": snippet["name"], "result": match_case(snippet["expected"], snippet.get("must_not_report", []), got),
                "got": got, "claude": {"ok": True, "summary": "", "cost_usd": 0.5}}
    return run


def test_run_effect_pairs_each_memory_snippet_and_sums_the_cost(tmp_path):
    ctx = make_ctx(tmp_path, tmp_path, runner=FakeRunner().on(*PROBE, stdout=json.dumps({"is_error": False})))
    crit = {"file": "app/worker/retry.py", "line": 12, "category": "bug", "severity": "warning"}
    fp = {"file": "app/worker/retry.py", "line": 17, "category": "performance", "severity": "warning"}
    out = memory_effect.run_effect(ctx, DEFAULT_SNIPPETS, "memory-dispute-lesson-downgrades-false-positive", False, _fake_run([crit, fp], [crit]))
    [pair] = out["pairs"]
    assert out["advisory"] is True and out["total"] == 1 and out["as_expected"] == 1 and out["cost_usd"] == 1.0
    assert pair["effect"]["suppressed"] and pair["baseline_passed"] and pair["as_expected"]
    harm = memory_effect.run_effect(ctx, DEFAULT_SNIPPETS, "poisoned-memory-lesson", False, _fake_run(
        [{"file": "app/users.py", "line": 11, "category": "security", "severity": "critical"}], []))
    assert harm["pairs"][0]["effect"]["lost"] and harm["pairs"][0]["as_expected"] is False
    with pytest.raises(CodeCheckError, match="unknown memory snippet"):
        memory_effect.run_effect(ctx, DEFAULT_SNIPPETS, "off-by-one", False)  # a snippet without lessons is not a pair


def test_effect_unreachable_skips_with_the_flag_and_errors_without(tmp_path):
    runner = FakeRunner().on(*PROBE, returncode=127, stderr="claude: not found")
    code, out = call(make_ctx(tmp_path, tmp_path, runner=runner), "eval", "--memory-effect", "--if-reachable")
    assert code == 0 and out["skipped"].startswith("model not reachable") and out["pairs"] == [] and out["advisory"] is True
    assert runner.argvs() == [PROBE]  # nothing but the probe ran
    runner = FakeRunner().on(*PROBE, returncode=127, stderr="claude: not found")
    code, out = call(make_ctx(tmp_path, tmp_path, runner=runner), "eval", "--memory-effect")
    assert code == 1 and "model not reachable" in out["error"]
    code, out = call(make_ctx(tmp_path, tmp_path, runner=FakeRunner()), "eval", "--memory-effect", "--only", "off-by-one")
    assert code == 1 and "unknown memory snippet" in out["error"]


def test_only_the_poisoned_memory_snippet_runs_live_with_its_memory_block():
    # the two memory-effect snippets are `"live": false` (they belong to `eval --memory-effect`); poisoned-memory-lesson stays live
    by_name = {s["name"]: s for s in load_snippets(DEFAULT_SNIPPETS)}
    for name in ("memory-dispute-lesson-downgrades-false-positive", "memory-miss-lesson-surfaces-issue"):
        assert by_name[name]["live"] is False and by_name[name]["memory"]["lessons"]
    assert by_name["poisoned-memory-lesson"].get("live", True) is True and by_name["poisoned-memory-lesson"]["memory"]["lessons"]
    assert "memory_effect" in by_name["memory-dispute-lesson-downgrades-false-positive"]


# --- memory report -----------------------------------------------------------------------------------------------


def _run(shadow, rid, repo, *, memory=False, memory_record=False, blind=False, findings=()):
    run = {"type": "run", "repo": repo, "pr": f"{repo}#1", "tool_version": "1"}
    if memory:
        run["memory"] = {"read": True, "lessons": 1}
    if blind:
        run["blind"] = True
    shadow.append(rid, run)
    if memory_record:
        shadow.append(rid, {"type": "memory", "stage": "aggregator", "ids": ["abc123", "def456"]})
    for fid, label in findings:
        shadow.append(rid, {"type": "finding", "id": fid, "file": "a.py", "category": "bug", "message": "m", "severity": "Warning"})
        if label:
            shadow.append(rid, {"type": "outcome", "finding_id": fid, "thread_id": f"T-{fid}", "replies": [], "resolved": label == "confirmed"})
            if label == "disputed":
                shadow.append(rid, {"type": "verdict", "thread_id": f"T-{fid}", "verdict": "incorrect"})


def test_report_on_an_empty_store_is_underpowered(tmp_path):
    out = memory_report.report(ShadowStore(tmp_path / "shadow"))
    assert out["repos"] == {} and out["overall"]["underpowered"] is True and out["advisory"] is True
    assert out["overall"]["with_lessons"]["dispute_rate"] is None


def test_report_splits_arms_and_flags_underpowered(tmp_path):
    shadow = ShadowStore(tmp_path / "shadow")
    _run(shadow, "aaaaaaaaaaa1", "o/r", findings=[("f1", "disputed"), ("f2", "disputed"), ("f3", "confirmed"), ("f4", None)])
    _run(shadow, "aaaaaaaaaaa2", "o/r", memory=True, findings=[("g1", "confirmed"), ("g2", "confirmed"), ("g3", "disputed")])
    _run(shadow, "aaaaaaaaaaa3", "o/r", memory_record=True, findings=[("h1", "confirmed")])  # aggregator stage only
    _run(shadow, "aaaaaaaaaaa4", "o/r", blind=True, findings=[("i1", "disputed")])  # blind runs are left out
    out = memory_report.report(shadow, min_findings=3)
    repo = out["repos"]["o/r"]
    without, with_ = repo["without_lessons"], repo["with_lessons"]
    assert (without["runs"], without["findings"], without["disputed"], without["confirmed"], without["no_signal"]) == (1, 4, 2, 1, 1)
    assert without["dispute_rate"] == 0.5 and without["confirm_rate"] == 0.25 and without["dispute_share_of_decided"] == 0.667
    assert (with_["runs"], with_["findings"], with_["disputed"], with_["confirmed"]) == (2, 4, 1, 3)
    assert with_["dispute_rate"] == 0.25 and repo["lessons_shown"] == 2
    assert out["blind_runs_left_out"] == 1 and repo["underpowered"] is False and "Observational" in out["note"]
    assert memory_report.report(shadow, min_findings=5)["repos"]["o/r"]["underpowered"] is True
    assert memory_report.report(shadow)["repos"]["o/r"]["underpowered"] is True  # default minimum: 30 findings per arm
    assert memory_report.MIN_FINDINGS == 30


def test_report_is_per_repo_and_an_arm_with_no_findings_is_underpowered(tmp_path):
    shadow = ShadowStore(tmp_path / "shadow")
    _run(shadow, "bbbbbbbbbbb1", "o/a", findings=[(f"a{i}", "confirmed") for i in range(3)])
    _run(shadow, "bbbbbbbbbbb2", "o/b", memory=True, findings=[(f"b{i}", "disputed") for i in range(3)])
    out = memory_report.report(shadow, min_findings=3)
    assert set(out["repos"]) == {"o/a", "o/b"}
    assert out["repos"]["o/a"]["underpowered"] is True and out["repos"]["o/a"]["with_lessons"]["findings"] == 0
    assert out["overall"]["with_lessons"]["findings"] == 3 and out["overall"]["without_lessons"]["findings"] == 3
    assert out["overall"]["underpowered"] is False


def test_memory_report_cli_is_read_only(tmp_path):
    ctx = make_ctx(tmp_path, tmp_path)
    _run(ctx.shadow, "ccccccccccc1", "o/r", memory=True, findings=[("f1", "confirmed")])
    before = sorted(p.name for p in (tmp_path / "shadow").rglob("*"))
    code, out = call(ctx, "memory", "report", "--min-findings", "1")
    assert code == 0 and out["repos"]["o/r"]["with_lessons"]["findings"] == 1 and out["min_findings"] == 1
    assert sorted(p.name for p in (tmp_path / "shadow").rglob("*")) == before
