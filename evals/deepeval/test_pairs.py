"""The pair file must stay resolvable against the golden cases (no model, no network)."""

import json

import pytest
from pairs import HERE, LABELS, STAGES, load_pairs, resolve

RAW = json.loads((HERE / "pairs.json").read_text())


def test_every_pair_resolves_to_one_human_comment_and_one_candidate():
    assert len(load_pairs()) == len(RAW)


def test_ids_are_unique():
    ids = [p["id"] for p in RAW]
    assert len(ids) == len(set(ids))


def test_labels_and_stages_are_known():
    for p in RAW:
        assert p["my_label"] in LABELS
        assert p["owner_label"] is None or p["owner_label"] in LABELS
        assert p["candidate"]["stage"] in STAGES


def test_controls_are_expected_to_differ():
    controls = [p for p in RAW if p["control"]]
    assert controls
    assert all(p["my_label"] == "different" for p in controls)


def test_a_broken_reference_is_reported_not_guessed():
    broken = {**RAW[0], "human_line": 999999}
    with pytest.raises(LookupError):
        resolve(broken)
