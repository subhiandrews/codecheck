"""Unit tests for the F1 baseline classifier, on synthetic cases (no golden data, no model)."""

from f1_baseline import classify, summarize

PATH = "src/a.rs"


def _case(raw=(), kept=(), dropped=()):
    return {
        "reviewer_outputs": {"skeptic": [{"file": f, "line": n, "message": "m"} for f, n in raw]},
        "aggregator": {
            "findings": [{"file": f, "line": n, "message": "m"} for f, n in kept],
            "dropped": [{"file": f, "line": n, "reason": "low-value", "message": "m"} for f, n in dropped],
        },
    }


def _stage(case, line=100, window=5):
    return classify(case, {"path": PATH, "line": line}, window)["stage"]


def test_kept_wins_over_every_earlier_stage():
    case = _case(raw=[(PATH, 100)], kept=[(PATH, 102)], dropped=[(PATH, 100)])
    assert _stage(case) == "kept"


def test_dropped_when_only_the_aggregator_dropped_it():
    assert _stage(_case(raw=[(PATH, 100)], dropped=[(PATH, 99)])) == "dropped"


def test_raw_only_when_never_kept_or_dropped():
    assert _stage(_case(raw=[(PATH, 104)])) == "raw_only"


def test_window_edge_is_inclusive():
    assert _stage(_case(raw=[(PATH, 105)]), window=5) == "raw_only"
    assert _stage(_case(raw=[(PATH, 106)]), window=5) == "same_file"


def test_other_file_is_not_a_match():
    assert _stage(_case(raw=[("src/b.rs", 100)])) == "not_raised"


def test_missing_line_never_matches_nearby():
    result = classify(_case(raw=[(PATH, 100)]), {"path": PATH, "line": None})
    assert result["stage"] == "same_file"


def test_summary_counts_every_stage():
    rows = [{"case": "c", "stage": "dropped"}, {"case": "c", "stage": "dropped"}, {"case": "d", "stage": "kept"}]
    summary = summarize(rows)
    assert summary["total"]["dropped"] == 2
    assert summary["by_case"] == {"c": {"dropped": 2}, "d": {"kept": 1}}
