from codecheck.core.diffmap import parse_diff
from codecheck.core.findings import finding_id
from codecheck.core.pipeline import ReviewPlan, finalize_findings, plan_review
from codecheck.models import Finding, Severity

DIFF = """diff --git a/calc.py b/calc.py
new file mode 100644
--- /dev/null
+++ b/calc.py
@@ -0,0 +1,4 @@
+def add(a, b):
+    return a - b
+def div(a, b):
+    return a / b
"""


def _f(line, sev=Severity.WARNING, message="m", file="calc.py", reviewers=("r",)):
    return Finding(file=file, line=line, severity=sev, category="logic", message=message, reviewers=reviewers)


def test_finalize_anchors_merges_and_sorts():
    files = parse_diff(DIFF)
    out = finalize_findings([
        _f(4, Severity.SUGGESTION, "late"),
        _f(2, Severity.WARNING, "dup", reviewers=("a",)),
        _f(2, Severity.CRITICAL, "dup", reviewers=("b",)),
        _f(1, Severity.WARNING, "early"),
    ], files)
    assert [(f.line, f.severity, f.message) for f in out] == [
        (2, Severity.CRITICAL, "dup"), (1, Severity.WARNING, "early"), (4, Severity.SUGGESTION, "late")]
    assert out[0].reviewers == ("a", "b") and out[0].anchor == "    return a - b"


def test_plan_review_caps_and_skips_repeats():
    files = parse_diff(DIFF)
    findings = finalize_findings([
        _f(1, Severity.CRITICAL, "first"), _f(2, Severity.WARNING, "second"),
        _f(3, Severity.WARNING, "seen before"), _f(40, Severity.WARNING, "far away"),
    ], files)
    already = {finding_id(f) for f in findings if f.message == "seen before"}
    plan = plan_review(findings, files, already=already, cap=1)
    assert isinstance(plan, ReviewPlan)
    assert [(f.message, a.line) for f, a in plan.inline] == [("first", 1)]
    # Unanchorable findings come first, then the ones the cap pushed out, as `post` lists them.
    assert [f.message for f in plan.not_inline] == ["far away", "second"]
    assert plan.repeated == 1
