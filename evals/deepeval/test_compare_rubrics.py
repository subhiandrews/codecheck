from __future__ import annotations

from compare_rubrics import RUBRICS, summarize


def _pair(pair_id: str, truth: str, source: str = "owner", control: bool = False):
    return {"id": pair_id, "truth": truth, "source": source, "control": control}


def _results(v1: dict[str, str], v2: dict[str, str]):
    return {
        "v1": {k: {"label": v} for k, v in v1.items()},
        "v2": {k: {"label": v} for k, v in v2.items()},
    }


def test_summarize_reports_each_rubric_for_owner_and_all_pairs():
    pairs = [_pair("a", "same"), _pair("b", "different"), _pair("c", "same", source="mine")]
    results = _results({"a": "related", "b": "different", "c": "same"}, {"a": "same", "b": "related", "c": "same"})
    out = summarize(pairs, results)
    assert set(out) == set(RUBRICS)
    assert out["v1"]["owner"][0]["agree"] == 2 and out["v1"]["owner"][0]["of"] == 2  # related folded as same
    assert out["v1"]["owner"][1]["disagree"] == ["a"]  # related folded as different
    assert out["v2"]["all"][0]["of"] == 3
    assert out["v2"]["owner"][1]["disagree"] == []


def test_summarize_flags_a_control_the_judge_says_is_not_different():
    pairs = [_pair("c1", "different", control=True), _pair("c2", "different", control=True)]
    results = _results({"c1": "different", "c2": "related"}, {"c1": "different", "c2": "different"})
    out = summarize(pairs, results)
    assert out["v1"]["controls_wrong"] == ["c2"]
    assert out["v2"]["controls_wrong"] == []


def test_summarize_counts_errors_and_leaves_them_out():
    pairs = [_pair("a", "same"), _pair("b", "same")]
    results = {"v1": {"a": {"label": "same"}, "b": {"error": "boom"}}, "v2": {"a": {"label": "same"}, "b": {"label": "same"}}}
    out = summarize(pairs, results)
    assert out["v1"]["errors"] == 1 and out["v1"]["all"][0]["of"] == 1
    assert out["v2"]["errors"] == 0
