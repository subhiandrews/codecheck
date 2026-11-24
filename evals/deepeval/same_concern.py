"""A GEval metric for "is the candidate finding the same concern as the human comment?".

The judge scores 0-10 against fixed steps (so one judge call per pair, and the rubric is readable
here). DeepEval turns that into 0-1. `label_for` maps the score to same/related/different, and
`agreement` compares the judge with a person's labels, counting "related" both ways.

Usage (costs tokens, needs ANTHROPIC_API_KEY in the environment):
  python -B evals/deepeval/same_concern.py --ids P4,P7,C1,C2          # prints the plan, calls nothing
  python -B evals/deepeval/same_concern.py --ids P4,P7,C1,C2 --yes    # runs the judge
  python -B evals/deepeval/same_concern.py --yes --against mine       # all pairs, my labels
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import _env  # noqa: F401  (telemetry opt-out, must run before deepeval is imported)
from pairs import load_pairs

SAME_AT = 0.9
RELATED_AT = 0.4

STEPS = [
    (
        "Read the human review comment (input) and the automated finding (actual output). Ignore severity, "
        "tone, length and which file each sits in."
    ),
    (
        "State in one sentence the concern each one raises. A code suggestion with no stated concern raises "
        "no concern."
    ),
    (
        "Decide whether both ask for the same change for the same reason. The same topic or the same line "
        "is not enough. A different mechanism, a different fix or a different ask is not the same concern."
    ),
    (
        "Score 9-10 if it is one concern, even if worded differently. Score 4-8 if they share an area or "
        "cause but differ in mechanism or ask. Score 0-3 if they are different concerns."
    ),
]


# v2 judges the underlying problem and sets the proposed fix aside; v1 above treats a different fix as a
# different concern. Written before compare_rubrics.py ran either, so the comparison cannot be tuned after the fact.
STEPS_V2 = [
    (
        "Read the human review comment (input) and the automated finding (actual output). Ignore severity, "
        "tone, length, and which file or line each sits on."
    ),
    (
        "For each, state in one sentence the underlying problem it points at: what is wrong or at risk in the "
        "code. Set aside the fix it proposes. A request with no problem behind it (a formatting request, a bare "
        "code suggestion, a question that exposes no problem) points at no problem."
    ),
    (
        "Decide whether both point at the same underlying problem. The same problem is the same concern even "
        "when the two propose different fixes, or one is a question and the other a statement. The same topic, "
        "the same code or the same line is not enough."
    ),
    (
        "Score 9-10 if they point at the same underlying problem. Score 4-8 if they are closely connected: one "
        "problem is a cause or consequence of the other, or fixing one would change the other. Score 0-3 if "
        "they are different problems, even in the same code or on the same topic."
    ),
]


def label_for(score: float) -> str:
    if score >= SAME_AT:
        return "same"
    return "related" if score >= RELATED_AT else "different"


def agreement(pairs: list[dict], predicted: dict[str, str], truth: dict[str, str], related_as: str) -> dict:
    """Agreement with `related` folded into `related_as` ('same' or 'different') on both sides."""

    def fold(label: str) -> str:
        return related_as if label == "related" else label

    misses = [p["id"] for p in pairs if fold(predicted[p["id"]]) != fold(truth[p["id"]])]
    return {"related_as": related_as, "agree": len(pairs) - len(misses), "of": len(pairs), "disagree": misses}


def build_metric(judge, steps=None):
    from deepeval.metrics import GEval
    from deepeval.test_case import SingleTurnParams

    return GEval(
        name="Same concern",
        evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT],
        evaluation_steps=steps or STEPS,
        model=judge,
        async_mode=False,
    )


def run(pairs: list[dict], judge) -> list[dict]:
    from deepeval.test_case import LLMTestCase

    metric = build_metric(judge)
    rows = []
    for p in pairs:
        metric.measure(LLMTestCase(input=p["human_text"], actual_output=p["candidate_text"]))
        rows.append({"id": p["id"], "score": metric.score, "label": label_for(metric.score), "reason": metric.reason})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ids", help="comma-separated pair ids; default all")
    ap.add_argument("--against", choices=["owner", "mine"], default="owner")
    ap.add_argument("--yes", action="store_true", help="actually call the judge")
    args = ap.parse_args()

    pairs = load_pairs()
    if args.ids:
        want = set(args.ids.split(","))
        pairs = [p for p in pairs if p["id"] in want]
    key = "owner_label" if args.against == "owner" else "my_label"
    unlabelled = [p["id"] for p in pairs if not p[key]]
    if unlabelled:
        print(f"no {key} for {unlabelled}; add them or pass --against mine", file=sys.stderr)
        return 2

    from judge import AnthropicJudge

    judge = AnthropicJudge()
    print(f"{len(pairs)} pairs, {len(pairs)} judge calls to {judge.get_model_name()}, vs {key}")
    if not args.yes:
        print("dry run: pass --yes to call the judge")
        return 0

    rows = run(pairs, judge)
    predicted = {r["id"]: r["label"] for r in rows}
    truth = {p["id"]: p[key] for p in pairs}
    for r in rows:
        print(f"{r['id']}: score {r['score']:.2f} -> {r['label']} (labelled {truth[r['id']]})")
    report = {
        "model": judge.get_model_name(),
        "against": key,
        "rows": rows,
        "agreement": [agreement(pairs, predicted, truth, m) for m in ("same", "different")],
        "controls_wrong": [p["id"] for p in pairs if p["control"] and predicted[p["id"]] != "different"],
    }
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / "same_concern_latest.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({k: report[k] for k in ("agreement", "controls_wrong")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
