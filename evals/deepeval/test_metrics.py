from __future__ import annotations

import asyncio

import _env  # noqa: F401  (telemetry opt-out, must run before deepeval is imported)
from deepeval.test_case import LLMTestCase
from metrics import (
    AggregatorAccounting,
    AnchoringShare,
    FindingsFileWritten,
    MessageLength,
    NoCommentCapFindings,
    NoProcessVoice,
    default_metrics,
)

DIFF = """diff --git a/a.py b/a.py
index 1111111..2222222 100644
--- a/a.py
+++ b/a.py
@@ -1,3 +1,4 @@
 keep1
+added2
 keep3
 keep4
"""


def _case(meta: dict) -> LLMTestCase:
    return LLMTestCase(input="run", actual_output="x", metadata=meta)


def _kept(*messages: str) -> list[dict]:
    return [{"message": m} for m in messages]


def test_anchoring_share_counts_findings_on_an_added_line():
    meta = {"diff": DIFF, "reviewers": {"skeptic": [{"file": "a.py", "line": 2}, {"file": "a.py", "line": 3}, {"file": "b.py", "line": 1}]}}
    assert AnchoringShare.applies(meta)
    metric = AnchoringShare()
    assert metric.measure(_case(meta)) == 1 / 3
    assert "added 1" in metric.reason and "file_not_in_diff 1" in metric.reason
    assert not AnchoringShare.applies({"diff": DIFF, "reviewers": {"skeptic": []}})


def test_no_process_voice_flags_the_aggregators_own_reasoning():
    meta = {"kept": _kept("Downgraded from warning: I could not confirm that.", "A plain finding.")}
    metric = NoProcessVoice(threshold=1.0)
    assert metric.measure(_case(meta)) == 0.5
    assert metric.success is False and metric.is_successful() is False
    assert "downgraded" in metric.reason
    assert not NoProcessVoice.applies({"kept": []})


def test_message_length_only_reports_without_a_target():
    metric = MessageLength(max_chars=600)
    assert metric.measure(_case({"kept": _kept("x" * 10, "y" * 700)})) == 0.5
    assert metric.success is None and "longest 700" in metric.reason
    assert "600" in metric.__name__


def test_comment_cap_findings_are_found_by_their_wording():
    meta = {"kept": _kept("The added comments are over the cap: 3 comment lines against 43.", "A real bug.")}
    metric = NoCommentCapFindings(threshold=1.0)
    assert metric.measure(_case(meta)) == 0.5 and metric.success is False


def test_comment_cap_needs_a_docs_finding_that_names_the_cap():
    docs = lambda m: {"category": "docs", "message": m}
    meta = {"kept": [
        docs("Comment cap exceeded: 5 comment lines out of 51 added lines (9.8%, cap 3%)."),
        docs("Comments make up 11.2% of the added lines, which is over the 3% cap."),
        docs("The comments are stale after this rename."),  # about comments, not the cap
        {"category": "correctness", "message": "comment_flags starts each run with an empty state, so comment lines are miscounted."},
        {"category": "security", "message": "There is no cap on the request size."},
    ]}
    metric = NoCommentCapFindings(threshold=1.0)
    assert metric.measure(_case(meta)) == 3 / 5 and "2 comment-cap findings" in metric.reason


def test_process_voice_ignores_ordinary_review_prose():
    prose = ["The input is not verified before use.", "I did not see a bounds check here, so a long value overruns it.",
             "This cap is downgraded in the docs only."]
    assert NoProcessVoice().measure(_case({"kept": _kept(*prose)})) == 1.0
    notes = ["Downgraded from warning: unconfirmed.", "Lowered from critical because it is test-only.",
             "I could not find a caller.", "I couldn't reproduce it.", "The reviewers said it fails on Oracle."]
    metric = NoProcessVoice()
    assert metric.measure(_case({"kept": _kept(*notes)})) == 0.0 and "downgraded from" in metric.reason


def test_metrics_set_every_attribute_deepeval_defines_on_its_base():
    from deepeval.metrics import BaseMetric
    metric = NoProcessVoice()
    public = [a for a in dir(BaseMetric) if not a.startswith("_") and not callable(getattr(BaseMetric, a, None))]
    assert [a for a in public if not hasattr(metric, a)] == []  # a new required attribute in DeepEval fails here, not in a run


def test_findings_file_written_names_the_reviewers_that_skipped_it():
    metric = FindingsFileWritten(threshold=1.0)
    assert metric.measure(_case({"reviewer_output": {"a": True, "b": False}})) == 0.5
    assert metric.reason == "no file from b"
    assert not FindingsFileWritten.applies({"reviewer_output": None})


def test_accounting_counts_a_reviewer_once_per_item_and_flags_a_shortfall():
    reviewers = {"a": [{"file": "x"}, {"file": "y"}], "b": [{"file": "z"}]}
    metric = AggregatorAccounting()
    full = {"reviewers": reviewers, "kept": [{"reviewers": ["a"]}], "dropped": [{"reviewers": ["a", "b"]}]}
    assert metric.measure(_case(full)) == 1.0
    short = {"reviewers": reviewers, "kept": [{"reviewers": ["a"]}], "dropped": []}
    assert metric.measure(_case(short)) == 1 / 3
    assert not AggregatorAccounting.applies({"reviewers": reviewers, "kept": [], "dropped": []})


def test_accounting_agrees_with_the_hand_off_rule_on_the_same_input():
    from codecheck.commands.handoff import _accounting

    cases = [
        ({"a": [{}, {}], "b": [{}]}, [{"reviewers": ["a"]}], [{"reviewers": ["a", "b"]}]),  # everything named
        ({"a": [{}, {}], "b": [{}]}, [{"reviewers": ["a"]}], []),  # b is never named
        ({"a": [{}, {}, {}]}, [{"reviewers": ["a"]}], []),  # three findings merged into one item
        ({"a": [{}], "b": []}, [{"reviewers": ["a", "a", 7, None]}, "oops", 3], [{"reviewers": "a"}]),  # malformed items
        ({"a": "not a list", "b": [{}]}, [{"reviewers": ["b"]}], []),  # a reviewer whose output is not a list
    ]
    for reviewers, kept, dropped in cases:
        meta = {"reviewers": reviewers, "kept": kept, "dropped": dropped}
        assert AggregatorAccounting.applies(meta), meta
        theirs = _accounting(reviewers, kept, dropped)
        short = theirs["possibly_unaccounted"]
        metric = AggregatorAccounting()
        metric.measure(_case(meta))
        assert metric.denominator == theirs["reviewer_findings"], meta
        assert metric.numerator == theirs["reviewer_findings"] - sum(short.values()), meta
        assert (metric.reason == f"possibly unaccounted {short}") if short else metric.reason == "all named", meta


def test_metrics_follow_deepevals_async_and_name_contract():
    metric = NoProcessVoice(threshold=1.0)
    assert asyncio.run(metric.a_measure(_case({"kept": _kept("fine")}))) == 1.0
    assert metric.__name__ == metric.name


def test_default_targets_match_the_agreed_ones():
    targets = {type(m).__name__: m.threshold for m in default_metrics()}
    assert targets["NoCommentCapFindings"] == 1.0 and targets["FindingsFileWritten"] == 1.0
    assert targets["NoProcessVoice"] == 1.0
    assert targets["AnchoringShare"] is None and targets["MessageLength"] is None and targets["AggregatorAccounting"] is None
