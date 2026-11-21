"""Run two "same concern?" rubrics on every labelled pair and compare each with the labels.

Labelled pairs are the hand-made ones in pairs.json (the owner's label when there is one, otherwise the
author's, marked source "mine") plus real hand-off pairs the owner labelled in handoff_labels.json. v1 is
the original rubric, v2 sets the proposed fix aside and judges the underlying problem. One run, no tuning.

Usage (costs tokens, needs ANTHROPIC_API_KEY in the environment):
  python -B evals/deepeval/compare_rubrics.py          # counts the calls, calls nothing
  python -B evals/deepeval/compare_rubrics.py --yes    # runs both rubrics on every pair
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from pairs import load_pairs
from same_concern import STEPS, STEPS_V2, agreement, build_metric, label_for

OUT = Path("~/.local/share/code-check-evals").expanduser()
RUBRICS = {"v1": STEPS, "v2": STEPS_V2}
WORKERS = 4


def labelled_pairs() -> list[dict]:
    pairs = [
        {"id": p["id"], "human_text": p["human_text"], "candidate_text": p["candidate_text"], "control": p["control"],
         "truth": p["owner_label"] or p["my_label"], "source": "owner" if p["owner_label"] else "mine"}
        for p in load_pairs()
    ]
    labels = json.loads((OUT / "handoff_labels.json").read_text())
    rows = {r["id"]: r for r in json.loads((OUT / "handoff_judgement.json").read_text())}
    for pair_id, entry in labels.items():
        if "label" in entry and pair_id in rows:
            pairs.append({"id": pair_id, "human_text": rows[pair_id]["human_text"], "control": False,
                          "candidate_text": rows[pair_id]["candidate_text"], "truth": entry["label"], "source": "owner"})
    return pairs


def judge_one(pair: dict, name: str, judge) -> dict:
    from deepeval.test_case import LLMTestCase

    metric = build_metric(judge, RUBRICS[name])  # one per call: a metric keeps its last score on itself
    try:
        metric.measure(LLMTestCase(input=pair["human_text"], actual_output=pair["candidate_text"]))
    except Exception as e:  # noqa: BLE001 - one failed call must not lose the others
        return {"error": f"{type(e).__name__}: {e}"[:300]}
    return {"score": metric.score, "label": label_for(metric.score), "reason": metric.reason}


def summarize(pairs: list[dict], results: dict[str, dict[str, dict]]) -> dict:
    """Per rubric, agreement with the labels (related folded both ways) for the owner's labels and for all."""
    out: dict = {}
    for name in RUBRICS:
        scored = [p for p in pairs if "label" in results[name][p["id"]]]
        predicted = {p["id"]: results[name][p["id"]]["label"] for p in scored}
        truth = {p["id"]: p["truth"] for p in scored}
        out[name] = {
            "errors": len(pairs) - len(scored),
            "controls_wrong": [p["id"] for p in scored if p["control"] and predicted[p["id"]] != "different"],
            **{
                subset: [agreement(chosen, predicted, truth, fold) for fold in ("same", "different")]
                for subset, chosen in (("owner", [p for p in scored if p["source"] == "owner"]), ("all", scored))
            },
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="actually call the judge")
    args = ap.parse_args()

    pairs = labelled_pairs()
    from judge import AnthropicJudge

    judge = AnthropicJudge()
    calls = len(pairs) * len(RUBRICS)
    print(f"{len(pairs)} labelled pairs ({sum(p['source'] == 'owner' for p in pairs)} owner), {calls} judge calls to {judge.get_model_name()}")
    if not args.yes:
        print("dry run: pass --yes to call the judge")
        return 0

    jobs = [(p, name) for p in pairs for name in RUBRICS]
    with ThreadPoolExecutor(WORKERS) as pool:
        done = list(pool.map(lambda job: judge_one(job[0], job[1], judge), jobs))
    results: dict[str, dict[str, dict]] = {name: {} for name in RUBRICS}
    for (pair, name), result in zip(jobs, done, strict=True):
        results[name][pair["id"]] = result
    summary = summarize(pairs, results)
    report = {
        "model": judge.get_model_name(),
        "rubrics": RUBRICS,
        "rows": [{k: p[k] for k in ("id", "source", "truth", "control")} | {name: results[name][p["id"]] for name in RUBRICS} for p in pairs],
        "summary": summary,
    }
    OUT.mkdir(mode=0o700, exist_ok=True)
    target = OUT / "rubric_comparison.json"
    target.write_text(json.dumps(report, indent=1))
    target.chmod(0o600)
    for p in pairs:
        cells = "  ".join(f"{n} {results[n][p['id']].get('score', float('nan')):.1f} {results[n][p['id']].get('label', 'ERR'):9}" for n in RUBRICS)
        print(f"{p['id']:26} {p['source']:5} truth={p['truth']:9} {cells}")
    print(json.dumps(summary, indent=1))
    print("wrote", os.fspath(target))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
