from __future__ import annotations

import pytest
from same_concern import agreement, label_for


@pytest.mark.parametrize(
    ("score", "label"),
    [(1.0, "same"), (0.9, "same"), (0.8, "related"), (0.4, "related"), (0.3, "different"), (0.0, "different")],
)
def test_label_for_cutoffs(score, label):
    assert label_for(score) == label


def test_agreement_counts_related_both_ways():
    pairs = [{"id": "P1"}, {"id": "P2"}, {"id": "P3"}]
    predicted = {"P1": "same", "P2": "related", "P3": "different"}
    truth = {"P1": "same", "P2": "same", "P3": "related"}
    as_same = agreement(pairs, predicted, truth, "same")
    as_different = agreement(pairs, predicted, truth, "different")
    assert (as_same["agree"], as_same["disagree"]) == (2, ["P3"])
    assert (as_different["agree"], as_different["disagree"]) == (2, ["P2"])
