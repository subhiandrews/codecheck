from codecheck.evals.match import RECALL_BAR, TOLERANCE, match_case


def _f(line, severity="warning", category="correctness", file="a.py"):
    return {"file": file, "line": line, "category": category, "severity": severity}


def test_pass_bar_and_tolerance_defaults():
    assert (TOLERANCE, RECALL_BAR) == (2, 0.8)


def test_match_within_tolerance_and_severity():
    assert match_case([_f(10)], [], [_f(12)])["matched"] == 1
    assert match_case([_f(10)], [], [_f(8)])["matched"] == 1
    res = match_case([_f(10)], [], [_f(13)])
    assert res["matched"] == 0 and res["passed"] is False and res["recall"] == 0.0


def test_other_file_or_category_does_not_match():
    assert match_case([_f(10)], [], [_f(10, file="b.py")])["matched"] == 0
    assert match_case([_f(10)], [], [_f(10, category="style")])["matched"] == 0


def test_category_aliases_match_case_insensitively():
    expected = [_f(10, category=["bug", "correctness"])]
    assert match_case(expected, [], [_f(10, category="Bug")])["matched"] == 1
    assert match_case(expected, [], [_f(10, category="CORRECTNESS")])["matched"] == 1
    assert match_case(expected, [], [_f(10, category="perf")])["matched"] == 0


def test_severity_mismatch_not_counted():
    res = match_case([_f(10, "critical")], [], [_f(10, "warning")])
    assert res["matched"] == 0 and res["passed"] is False
    assert res["severity_mismatches"] == [{"expected": _f(10, "critical"), "got": _f(10, "warning")}]


def test_recall_bar():
    expected = [_f(n * 10) for n in range(1, 6)]
    assert match_case(expected, [], [_f(n * 10) for n in range(1, 5)])["passed"] is True
    res = match_case(expected, [], [_f(n * 10) for n in range(1, 4)])
    assert res["passed"] is False and res["recall"] == 0.6 and (res["matched"], res["expected"]) == (3, 5)


def test_empty_expected_has_full_recall():
    res = match_case([], [], [_f(3, "suggestion")])
    assert res["recall"] == 1.0 and res["passed"] is True


def test_unexpected_critical_fails():
    crit = _f(40, "critical", category="security")
    res = match_case([_f(10)], [], [_f(10), crit])
    assert res["passed"] is False and res["unexpected_critical"] == [crit]
    # A Critical that sits on an expected Critical is expected.
    assert match_case([_f(40, "critical", category="security")], [], [crit])["unexpected_critical"] == []


def test_forbidden_hit_fails():
    rule = {"file": "a.py", "category": ["bug", "correctness"], "line": 5}
    res = match_case([], [rule], [_f(7)])
    assert res["passed"] is False and res["forbidden_hits"] == [{"rule": rule, "got": _f(7)}]
    assert match_case([], [rule], [_f(8)])["forbidden_hits"] == []  # outside the tolerance
    assert match_case([], [rule], [_f(5, category="style")])["forbidden_hits"] == []
    # A rule with only a file forbids any finding in it.
    assert match_case([], [{"file": "a.py"}], [_f(99, "suggestion", "style")])["passed"] is False


def test_got_finding_used_once():
    res = match_case([_f(10), _f(11)], [], [_f(10)])
    assert res["matched"] == 1 and res["recall"] == 0.5


def test_exact_severity_match_preferred_over_nearer_mismatch():
    # The nearer finding has the wrong severity; the right-severity one must still be matched.
    res = match_case([_f(10, "critical")], [], [_f(10, "warning"), _f(12, "critical")])
    assert res["matched"] == 1 and res["severity_mismatches"] == []
