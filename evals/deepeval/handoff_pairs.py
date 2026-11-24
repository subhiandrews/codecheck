"""Judge every human comment against every aggregator candidate in real hand-offs.

For each hand-off it pairs each human comment with each finding the aggregator kept and each it
dropped, then asks the "same concern?" metric (same_concern.py) about each pair. The judge never sees
whether a candidate was kept or dropped. Text comes from `<run>.raw.json` because the hand-off's own
copy of a kept finding is cut at 300 characters.

Output goes outside the repo (the shadow store is private): ~/.local/share/code-check-evals/.

Usage (costs tokens, needs ANTHROPIC_API_KEY in the environment):
  python -B evals/deepeval/handoff_pairs.py --runs RUN1,RUN2          # counts the pairs, calls nothing
  python -B evals/deepeval/handoff_pairs.py --runs RUN1,RUN2 --yes    # runs the judge
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

from same_concern import build_metric, label_for

STORE = Path(os.environ.get("CODE_CHECK_SHADOW_STORE", "~/.local/share/code-check/shadow")).expanduser()
OUT = Path("~/.local/share/code-check-evals").expanduser()
WORKERS = 4


def build_pairs(run_id: str, handoff: dict, raw: dict) -> list[dict]:
    """One pair per (human comment, kept or dropped finding). Empty when either side is empty."""
    comparison = handoff.get("comparison") or {}
    aggregator = raw.get("aggregator") or {}
    candidates = [("kept", f) for f in aggregator.get("findings", [])]
    candidates += [("dropped", f) for f in aggregator.get("dropped", [])]
    pairs = []
    for human in comparison.get("human_comments", []):
        for index, (stage, finding) in enumerate(candidates):
            pairs.append({
                "id": f"{run_id}:{human['thread_id'][-6:]}:{stage[0]}{index}",
                "run_id": run_id,
                "pr": handoff.get("pr"),
                "human_author": human["author"],
                "human_path": human["path"],
                "human_line": human["line"],
                "human_text": human["body"],
                "candidate_stage": stage,
                "candidate_file": finding["file"],
                "candidate_line": finding["line"],
                "candidate_text": finding["message"],
            })
    return pairs


def load_pairs(run_ids: list[str]) -> list[dict]:
    pairs = []
    for run_id in run_ids:
        handoff = json.loads((STORE / f"{run_id}.handoff" / "handoff.json").read_text())
        raw = json.loads((STORE / f"{run_id}.raw.json").read_text())
        pairs += build_pairs(run_id, handoff, raw)
    return pairs


def judge_pair(pair: dict, judge) -> dict:
    from deepeval.test_case import LLMTestCase

    metric = build_metric(judge)  # one per pair: a metric keeps its last score on itself
    try:
        metric.measure(LLMTestCase(input=pair["human_text"], actual_output=pair["candidate_text"]))
    except Exception as e:  # noqa: BLE001 - one failed call must not lose the others
        return {**pair, "error": f"{type(e).__name__}: {e}"[:300]}
    return {**pair, "score": metric.score, "label": label_for(metric.score), "reason": metric.reason}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", required=True, help="comma-separated hand-off run ids")
    ap.add_argument("--yes", action="store_true", help="actually call the judge")
    args = ap.parse_args()

    pairs = load_pairs(args.runs.split(","))
    from judge import AnthropicJudge

    judge = AnthropicJudge()
    print(f"{len(pairs)} pairs, {len(pairs)} judge calls to {judge.get_model_name()}, {WORKERS} at a time")
    if not args.yes:
        print("dry run: pass --yes to call the judge")
        return 0

    with ThreadPoolExecutor(WORKERS) as pool:
        rows = list(pool.map(lambda p: judge_pair(p, judge), pairs))
    OUT.mkdir(mode=0o700, exist_ok=True)
    target = OUT / "handoff_judgement.json"
    target.write_text(json.dumps(rows, indent=1))
    target.chmod(0o600)
    scored = [r for r in rows if "score" in r]
    counts = {label: sum(r["label"] == label for r in scored) for label in ("same", "related", "different")}
    print(json.dumps({"scored": len(scored), "errors": len(rows) - len(scored), **counts, "wrote": str(target)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
