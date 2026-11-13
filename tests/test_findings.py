from dataclasses import replace

import pytest

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
_NOTE = " (code suggestion withheld: it looked like it contained a secret)"


def test_security_finding_message_is_scrubbed_and_secret_suggestion_withheld():
    findings, errors = validate_findings([_SEC], reviewer="security")
    assert errors == []
    f = findings[0]
    assert f.message == "key is [REDACTED] in config" + _NOTE
    assert f.suggestion is None


def test_security_scrub_is_idempotent_so_ids_are_stable():
    first, _ = validate_findings([_SEC], reviewer="security")
    assert "[REDACTED]" in first[0].message
    again, _ = validate_findings([{**_SEC, "message": first[0].message, "suggestion": first[0].suggestion}],
                                 reviewer="aggregator")
    assert (again[0].message, again[0].suggestion) == (first[0].message, None)
    assert finding_id(again[0]) == finding_id(first[0])


@pytest.mark.parametrize("cat", ["security", "bug", "secrets", "insecurity"])
@pytest.mark.parametrize("code", [
    'API_KEY = os.environ["API_KEY"]',
    'password = get_secret("db")',
    "token: str = load_token()",
])
def test_realistic_code_suggestions_are_never_corrupted(cat, code):
    findings, _ = validate_findings([{**_SEC, "category": cat, "message": "read it from the env", "suggestion": code}],
                                    reviewer="r")
    f = findings[0]
    assert "[REDACTED]" not in (f.suggestion or "")
    assert f.suggestion in (code, None)
    if f.suggestion is None:
        assert f.message == "read it from the env" + _NOTE
    else:
        assert f.message == "read it from the env"


def test_known_realistic_suggestion_is_withheld_not_mangled():
    findings, _ = validate_findings(
        [{**_SEC, "message": "m", "suggestion": 'API_KEY = os.environ["API_KEY"]'}], reviewer="r")
    assert findings[0].suggestion is None
    assert findings[0].message == "m" + _NOTE


def test_suggestion_the_scrub_leaves_alone_is_kept_verbatim():
    findings, _ = validate_findings([{**_SEC, "message": "m", "suggestion": "x = compute()"}], reviewer="r")
    assert findings[0].suggestion == "x = compute()"
    assert findings[0].message == "m"


@pytest.mark.parametrize("cat", ["security", "bug", "secrets", "pii", "insecurity"])
def test_secret_in_message_is_scrubbed_for_every_category(cat):
    findings, _ = validate_findings([{**_SEC, "category": cat}], reviewer="r")
    assert findings[0].message == "key is [REDACTED] in config" + _NOTE
    assert findings[0].suggestion is None


@pytest.mark.parametrize("cat", ["security", "bug", "insecurity", "style"])
def test_ordinary_prose_is_unchanged_for_every_category(cat):
    msg = "Off by one: the loop skips the last element when n is even"
    findings, _ = validate_findings([{**_SEC, "category": cat, "message": msg, "suggestion": "range(n + 1)"}],
                                    reviewer="r")
    assert findings[0].message == msg
    assert findings[0].suggestion == "range(n + 1)"


def test_finding_without_suggestion_still_validates():
    findings, _ = validate_findings([{k: v for k, v in _SEC.items() if k != "suggestion"}], reviewer="r")
    assert findings[0].suggestion is None
    assert findings[0].message == "key is [REDACTED] in config"


@pytest.mark.parametrize("cat", ["Security", " SECURITY "])
def test_scrub_ignores_category_case_and_padding(cat):
    findings, _ = validate_findings([{**_SEC, "category": cat}], reviewer="r")
    assert findings[0].message == "key is [REDACTED] in config" + _NOTE
    assert findings[0].suggestion is None
