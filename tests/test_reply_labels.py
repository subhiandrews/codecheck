"""A reply on a finding's thread is a signal, not a verdict: `replied` until a verdict (or a resolve) classifies it.

Fixture: gitlab-org/cli !4487. Our only warning got "Thanks for catching this. <sha> gives an existing branch MR
precedence ..." from the author: an agreement and a fix, which must never become `not_expected` in a golden case.
"""

import json

from fakes import FakeProvider
from test_shadow_collect import REF, RID, seed, tc

from codecheck.models import Thread
from codecheck.shadow.collect import collect_outcomes
from codecheck.shadow.export import export_golden
from codecheck.shadow.store import ShadowStore

AGREED = "Thanks for catching this. `36d29ca1` gives an existing branch MR precedence and falls back to a full reference."
FID = "b" * 12


def labels_after(tmp_path, *verdicts, reply=AGREED, resolved=False):
    shadow = ShadowStore(tmp_path)
    seed(shadow)
    provider = FakeProvider()
    marker = f"**Warning**\n<!-- code-check:finding:{FID} -->"
    provider.threads[REF] = [Thread("T2", "a.py", 8, resolved, False, (tc(2, "me", marker), tc(3, "author", reply)))]
    collect_outcomes(provider, shadow, REF, "me")
    for v in verdicts:
        shadow.append(RID, {"type": "verdict", "thread_id": "T2", **v})
    out = tmp_path / "golden.jsonl"
    export_golden(shadow, out)
    (row,) = [json.loads(line) for line in out.read_text().splitlines()]
    return {f["id"]: f["label"] for f in row["findings"]}


def test_a_reply_alone_is_not_a_dispute(tmp_path):
    assert labels_after(tmp_path)[FID] == "replied"


def test_a_valid_verdict_on_a_replied_thread_confirms_the_finding(tmp_path):
    assert labels_after(tmp_path, {"verdict": "valid"})[FID] == "confirmed"


def test_an_incorrect_verdict_on_a_replied_thread_disputes_the_finding(tmp_path):
    assert labels_after(tmp_path, {"verdict": "incorrect"}, reply="this is not a bug")[FID] == "disputed"


def test_an_unclear_verdict_leaves_the_finding_replied(tmp_path):
    assert labels_after(tmp_path, {"verdict": "unclear"})[FID] == "replied"


def test_a_resolved_thread_is_still_confirmed_whatever_was_said(tmp_path):
    assert labels_after(tmp_path, resolved=True)[FID] == "confirmed"
