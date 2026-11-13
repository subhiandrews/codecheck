from dataclasses import replace

from codecheck.core.diffmap import parse_diff
from codecheck.core.findings import (
    attach_anchors,
    finding_id,
    merge_exact_duplicates,
    validate_findings,
)
from codecheck.models import Finding, Severity


def good(**over):
    base = {"file": "a.py", "line": 3, "severity": "Warning", "category": "bug", "message": "Off by one"}
    return {**base, **over}


def test_valid_findings_are_normalized_and_attributed():
    findings, errors = validate_findings([good(file="./a.py", suggestion="x = 1")], reviewer="skeptic")
    assert errors == []
    f = findings[0]
    assert (f.file, f.severity, f.reviewers, f.suggestion) == ("a.py", Severity.WARNING, ("skeptic",), "x = 1")


def test_root_prefix_is_stripped():
    findings, _ = validate_findings([good(file="/work/tree/a.py")], reviewer="r", root="/work/tree")
    assert findings[0].file == "a.py"


def test_invalid_items_report_errors_but_do_not_drop_valid_ones():
    raw = [good(), good(line=0), good(line=True), good(severity="blocker"), good(message=" "), "oops", good(file="")]
    findings, errors = validate_findings(raw, reviewer="r")
    assert len(findings) == 1
    assert len(errors) == 6
    assert all(e.startswith("r[") for e in errors)


def test_aggregator_supplied_reviewers_are_kept():
    findings, _ = validate_findings([good(reviewers=["skeptic", "lang-go"])], reviewer="aggregator")
    assert findings[0].reviewers == ("skeptic", "lang-go")


def test_non_list_is_one_error():
    findings, errors = validate_findings({"file": "a"}, reviewer="r")
    assert findings == [] and len(errors) == 1


def test_missing_category_defaults():
    item = good()
    del item["category"]
    findings, errors = validate_findings([item], reviewer="r")
    assert errors == [] and findings[0].category == "general"


def test_finding_id_is_stable_across_whitespace_and_case():
    a = Finding("a.py", 3, Severity.WARNING, "bug", "Off  by one", anchor="x = 1")
    b = replace(a, message="off by one", line=99, severity=Severity.CRITICAL)
    assert finding_id(a) == finding_id(b)
    assert finding_id(a) != finding_id(replace(a, file="b.py"))
    assert len(finding_id(a)) == 12


def test_merge_exact_duplicates_unions_reviewers_and_keeps_max_severity():
    a = Finding("a.py", 3, Severity.SUGGESTION, "bug", "Off by one", reviewers=("skeptic",))
    b = Finding("a.py", 3, Severity.CRITICAL, "bug", "off  by one", reviewers=("lang-python", "skeptic"))
    c = Finding("a.py", 4, Severity.WARNING, "bug", "Off by one", reviewers=("architect",))
    merged = merge_exact_duplicates([a, b, c])
    assert len(merged) == 2
    assert merged[0].severity is Severity.CRITICAL
    assert merged[0].reviewers == ("skeptic", "lang-python")


def test_attach_anchors_uses_the_changed_line_text():
    files = parse_diff("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1,2 @@\n x\n+y = 2\n")
    f = Finding("a.py", 2, Severity.WARNING, "bug", "m")
    assert attach_anchors([f], files)[0].anchor == "y = 2"
    assert attach_anchors([replace(f, file="zzz.py")], files)[0].anchor == ""


_SEC = {"file": "a.py", "line": 3, "severity": "critical", "category": "security",
        "message": "key is AKIAIOSFODNN7EXAMPLE in config", "suggestion": "rotate AKIAIOSFODNN7EXAMPLE now"}


def test_security_finding_text_is_scrubbed():
    findings, errors = validate_findings([_SEC], reviewer="security")
    assert errors == []
    f = findings[0]
    assert f.message == "key is [REDACTED] in config"
    assert f.suggestion == "rotate [REDACTED] now"
    assert "AKIAIOSFODNN7EXAMPLE" not in f.message + f.suggestion


def test_security_scrub_is_idempotent_so_ids_are_stable():
    first, _ = validate_findings([_SEC], reviewer="security")
    again, _ = validate_findings([{**_SEC, "message": first[0].message, "suggestion": first[0].suggestion}],
                                 reviewer="aggregator")
    assert (again[0].message, again[0].suggestion) == (first[0].message, first[0].suggestion)
    assert finding_id(again[0]) == finding_id(first[0])


def test_non_security_finding_text_is_not_scrubbed():
    findings, _ = validate_findings([{**_SEC, "category": "bug"}], reviewer="r")
    assert findings[0].message == "key is AKIAIOSFODNN7EXAMPLE in config"
    assert findings[0].suggestion == "rotate AKIAIOSFODNN7EXAMPLE now"


def test_security_finding_without_suggestion_still_validates():
    findings, _ = validate_findings([{k: v for k, v in _SEC.items() if k != "suggestion"}], reviewer="r")
    assert findings[0].suggestion is None


import pytest


@pytest.mark.parametrize("cat", ["Security", " SECURITY "])
def test_security_scrub_ignores_category_case_and_padding(cat):
    findings, _ = validate_findings([{**_SEC, "category": cat}], reviewer="r")
    assert findings[0].message == "key is [REDACTED] in config"
    assert findings[0].suggestion == "rotate [REDACTED] now"


@pytest.mark.parametrize("cat", ["insecurity", "bug"])
def test_scrub_match_is_exact_not_substring(cat):
    findings, _ = validate_findings([{**_SEC, "category": cat}], reviewer="r")
    assert findings[0].message == "key is AKIAIOSFODNN7EXAMPLE in config"
    assert findings[0].suggestion == "rotate AKIAIOSFODNN7EXAMPLE now"
