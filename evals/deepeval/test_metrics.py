from __future__ import annotations

import asyncio

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


def test_metrics_follow_deepevals_async_and_name_contract():
    metric = NoProcessVoice(threshold=1.0)
    assert asyncio.run(metric.a_measure(_case({"kept": _kept("fine")}))) == 1.0
    assert metric.__name__ == metric.name


def test_default_targets_match_the_agreed_ones():
    targets = {type(m).__name__: m.threshold for m in default_metrics()}
    assert targets["NoCommentCapFindings"] == 1.0 and targets["FindingsFileWritten"] == 1.0
    assert targets["NoProcessVoice"] == 1.0
    assert targets["AnchoringShare"] is None and targets["MessageLength"] is None and targets["AggregatorAccounting"] is None
