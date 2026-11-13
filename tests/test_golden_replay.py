import copy
import json
from pathlib import Path

import pytest
from clihelp import call, make_ctx

from codecheck.errors import CodeCheckError
from codecheck.evals.golden import (
    DEFAULT_CASES,
    check_case,
    load_case,
    load_cases,
    posted_view,
    run_replay,
)

GOLDEN = Path(__file__).resolve().parent / "golden"
SEED = GOLDEN / "seed-pipeline.json"


def _seed() -> dict:
    return json.loads(SEED.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", sorted(GOLDEN.glob("*.json")), ids=lambda p: p.stem)
def test_every_golden_case_replays(path):
    assert check_case(load_case(path)) == []


def test_default_cases_dir_is_the_repo_golden_dir():
    assert DEFAULT_CASES == GOLDEN


def test_seed_case_exercises_duplicates_and_summary_only_findings():
    case = _seed()
    aggregated = case["aggregator"]["findings"]
    assert len(aggregated) == len(case["expected_posted"]) + 1  # one exact duplicate merged away
    assert any(not f["inline"] for f in case["expected_posted"])


def test_check_case_reports_severity_change():
    case = _seed()
    case["expected_posted"][0]["severity"] = "suggestion"
    problems = check_case(case)
    f = case["expected_posted"][0]
    assert problems and any(f"{f['file']}:{f['line']}" in p and "severity" in p for p in problems), problems


def test_check_case_reports_missing_and_unexpected_findings():
    case = _seed()
    gone = case["expected_posted"].pop()
    case["expected_posted"].append({**gone, "line": 99})
    problems = check_case(case)
    assert any("unexpected" in p and f"{gone['file']}:{gone['line']}" in p for p in problems), problems
    assert any("not posted" in p and f"{gone['file']}:99" in p for p in problems), problems


def test_check_case_reports_decision_and_reviewer_drift():
    case = _seed()
    case["expected_decision"] = "APPROVE"
    member = next(iter(case["reviewer_outputs"]))
    case["reviewer_outputs"][member][0]["severity"] = "blocker"
    problems = check_case(case)
    assert any("decision" in p for p in problems), problems
    assert any(member in p and "validate" in p for p in problems), problems


def test_check_case_reports_an_invalid_aggregator_without_raising():
    case = copy.deepcopy(_seed())
    case["aggregator"] = {"findings": [{"file": "x.py"}]}
    problems = check_case(case)
    assert problems and "aggregator" in problems[0]


def test_load_cases_names_bad_file(tmp_path):
    (tmp_path / "broken-case.json").write_text(SEED.read_text()[:200])
    with pytest.raises(CodeCheckError, match="broken-case.json"):
        load_cases(tmp_path)


def test_load_cases_names_file_with_missing_key(tmp_path):
    case = _seed()
    del case["expected_posted"]
    (tmp_path / "no-expectations.json").write_text(json.dumps(case))
    with pytest.raises(CodeCheckError, match=r"no-expectations\.json.*expected_posted"):
        load_cases(tmp_path)


def test_load_cases_refuses_unconfirmed_labels(tmp_path):
    case = {**_seed(), "label_status": "auto"}
    (tmp_path / "auto.json").write_text(json.dumps(case))
    with pytest.raises(CodeCheckError, match="auto.json"):
        load_cases(tmp_path)


def test_run_replay_on_a_missing_directory_has_no_cases(tmp_path):
    assert run_replay(tmp_path / "nope") == {"passed": True, "cases": 0, "failed": []}


def test_eval_replay_command_reports_failures(tmp_path):
    cases = tmp_path / "cases"
    cases.mkdir()
    (cases / "good.json").write_text(SEED.read_text())
    bad = {**_seed(), "case_id": "drifted", "expected_decision": "APPROVE"}
    (cases / "bad.json").write_text(json.dumps(bad))
    ctx = make_ctx(tmp_path, tmp_path)
    code, out = call(ctx, "eval", "--replay", "--cases", str(cases))
    assert code == 0
    assert out["passed"] is False and out["cases"] == 2
    assert out["failed"][0]["case"] == "drifted" and out["failed"][0]["problems"]


def test_eval_replay_command_passes_on_the_repo_cases(tmp_path):
    code, out = call(make_ctx(tmp_path, tmp_path), "eval", "--replay")
    assert code == 0 and out["passed"] is True and out["cases"] >= 1, out


def test_eval_requires_a_mode(tmp_path, capsys):
    with pytest.raises(SystemExit):
        call(make_ctx(tmp_path, tmp_path), "eval")


def test_decision_check_flips_when_blocking_findings_are_downgraded():
    # The seed's COMMENT comes from its critical (and warning) findings, not from auto-approve being off.
    case = _seed()
    for f in case["aggregator"]["findings"]:
        f["severity"] = "suggestion"
    # all suggestions now, so they post in file/line order rather than by severity
    case["expected_posted"] = sorted(({**f, "severity": "suggestion"} for f in case["expected_posted"]),
                                     key=lambda f: (f["file"], f["line"]))
    assert case["expected_decision"] == "COMMENT"
    assert check_case(case) == ["decision APPROVE, expected COMMENT"]
    case["expected_decision"] = "APPROVE"
    assert check_case(case) == []


def test_posted_view_strips_the_run_root_like_post():
    case = _seed()
    root = "/work/trees/run-1"
    absolute = {**case["aggregator"], "findings": [{**f, "file": f"{root}/{f['file']}"} for f in case["aggregator"]["findings"]]}
    with pytest.raises(CodeCheckError, match="escapes the repository"):
        posted_view(absolute, case["diff"])
    assert posted_view(absolute, case["diff"], root=root) == posted_view(case["aggregator"], case["diff"])
