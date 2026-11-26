import json

import pytest

from codecheck.errors import CodeCheckError
from codecheck.evals import replay
from codecheck.evals.compare import compare_arms, load_results


def run(name, matched, comments=20, **extra):
    return {"name": name, "score": {"comments": comments, "matched": matched, "recall": matched / comments}, **extra}


def arm(name, *matched):
    return [run(name, m) for m in matched]


def only(report, name):
    return next(c for c in report["cases"] if c["name"] == name)


def test_an_arm_that_is_better_than_every_run_of_the_other_is_called_better():
    report = compare_arms(arm("pr6", 9, 10, 11), arm("pr6", 3, 4, 5))
    case = only(report, "pr6")
    assert case["verdict"] == "a-better" and report["verdict"] == "a-better"
    assert case["a"]["runs"] == 3 and case["a"]["mean"] == pytest.approx(0.5)
    assert (case["a"]["min"], case["a"]["max"]) == (0.45, 0.55)
    assert case["b"]["mean"] == pytest.approx(0.2)


def test_overlapping_runs_are_within_noise_even_when_the_means_differ():
    # one run per arm can look like a win; the spread of the same prompts (8 to 14 of 20) says it is not
    report = compare_arms(arm("pr6", 8, 12, 14), arm("pr6", 10, 11, 13))
    assert only(report, "pr6")["verdict"] == "within-noise" and report["verdict"] == "within-noise"


def test_fewer_than_three_runs_in_an_arm_is_too_few_to_say():
    report = compare_arms(arm("pr6", 9, 10), arm("pr6", 1, 2, 3))
    assert only(report, "pr6")["verdict"] == "too-few-runs" and report["verdict"] == "too-few-runs"
    assert compare_arms(arm("pr6", 9, 10), arm("pr6", 1, 2), min_runs=2)["verdict"] == "a-better"


def test_a_case_in_only_one_arm_is_reported_not_compared():
    report = compare_arms(arm("pr2", 5, 5, 5), arm("pr6", 5, 5, 5))
    assert {c["name"]: c["verdict"] for c in report["cases"]} == {"pr2": "missing-arm", "pr6": "missing-arm"}


def test_failed_and_dry_run_results_are_left_out_and_counted():
    a = arm("pr6", 9, 10, 11) + [{"name": "pr6", "error": "claude run failed"}, {"name": "pr6", "built": True}]
    report = compare_arms(a, arm("pr6", 1, 2, 3))
    case = only(report, "pr6")
    assert case["a"]["runs"] == 3 and case["a"]["skipped"] == 2 and case["verdict"] == "a-better"


def test_a_case_with_no_labelled_comments_has_no_recall_and_is_skipped():
    empty = {"name": "pr6", "score": {"comments": 0, "matched": 0, "recall": None}}
    report = compare_arms([empty] * 3, arm("pr6", 1, 2, 3))
    assert only(report, "pr6")["a"]["runs"] == 0 and only(report, "pr6")["verdict"] == "too-few-runs"


def test_one_case_each_way_is_mixed():
    a = arm("x", 9, 9, 9) + arm("y", 1, 1, 1)
    b = arm("x", 1, 1, 1) + arm("y", 9, 9, 9)
    assert compare_arms(a, b)["verdict"] == "mixed"


def test_load_results_reads_json_lines_and_names_a_bad_one(tmp_path):
    path = tmp_path / "r.jsonl"
    path.write_text(json.dumps(run("a", 1)) + "\n\n" + json.dumps(run("b", 2)) + "\n")
    assert [r["name"] for r in load_results(path)] == ["a", "b"]
    path.write_text("{not json}\n")
    with pytest.raises(CodeCheckError, match="line 1"):
        load_results(path)
    with pytest.raises(CodeCheckError, match="not readable"):
        load_results(tmp_path / "missing.jsonl")


def test_the_compare_subcommand_prints_the_report(tmp_path, capsys):
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    a.write_text("".join(json.dumps(r) + "\n" for r in arm("pr6", 9, 10, 11)))
    b.write_text("".join(json.dumps(r) + "\n" for r in arm("pr6", 3, 4, 5)))
    assert replay.main(["compare", str(a), str(b)]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "a-better"
    assert replay.main(["compare", str(a), str(tmp_path / "nope.jsonl")]) == 1
