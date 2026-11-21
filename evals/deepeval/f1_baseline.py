"""F1 baseline: for each human comment the panel missed, where was the concern lost?

Reads tests/golden/*.json (read-only). For every `misses` entry it looks for findings at the same
file within WINDOW lines and reports the furthest pipeline stage that held one:

  kept        an aggregator finding is nearby, so the concern may be in a posted finding
  dropped     no kept finding, but the aggregator dropped one nearby (the F1 loss)
  raw_only    a reviewer raised one nearby, but it is neither kept nor dropped
  same_file   nothing nearby, but the file has findings elsewhere
  not_raised  no finding in that file at all

This is location matching only. A nearby finding is a candidate, not proof that it is the same
concern; the candidate text is printed so a person (later, a judge) can decide.

Usage: .venv/bin/python -B evals/deepeval/f1_baseline.py [--window N] [--json]
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

GOLDEN = Path(__file__).resolve().parents[2] / "tests" / "golden"
WINDOW = 5
STAGES = ("kept", "dropped", "raw_only", "same_file", "not_raised")


def _close(a, b, window: int) -> bool:
    return isinstance(a, int) and isinstance(b, int) and abs(a - b) <= window


def _short(text, n: int = 110) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n] + "…"


def _raw_findings(case: dict) -> list[dict]:
    return [
        {**f, "reviewer": name}
        for name, findings in case.get("reviewer_outputs", {}).items()
        for f in findings
    ]


def classify(case: dict, miss: dict, window: int = WINDOW) -> dict:
    path, line = miss.get("path"), miss.get("line")
    aggregator = case.get("aggregator") or {}
    pools = {
        "kept": aggregator.get("findings", []),
        "dropped": aggregator.get("dropped", []),
        "raw_only": _raw_findings(case),
    }
    for stage, items in pools.items():
        near = [i for i in items if i.get("file") == path and _close(i.get("line"), line, window)]
        if near:
            return {"stage": stage, "candidates": near}
    in_file = [i for items in pools.values() for i in items if i.get("file") == path]
    return {"stage": "same_file" if in_file else "not_raised", "candidates": []}


def run(window: int = WINDOW, golden: Path = GOLDEN) -> list[dict]:
    rows = []
    for case_path in sorted(golden.glob("*.json")):
        case = json.loads(case_path.read_text())
        for miss in case.get("misses", []):
            verdict = classify(case, miss, window)
            rows.append(
                {
                    "case": case["case_id"],
                    "path": miss.get("path"),
                    "line": miss.get("line"),
                    "human": _short(miss.get("body")),
                    "stage": verdict["stage"],
                    "candidates": [
                        {
                            "line": c.get("line"),
                            "reason": c.get("reason"),
                            "reviewers": c.get("reviewers") or c.get("reviewer"),
                            "message": _short(c.get("message")),
                        }
                        for c in verdict["candidates"]
                    ],
                }
            )
    return rows


def summarize(rows: list[dict]) -> dict:
    by_case: dict[str, Counter] = {}
    for row in rows:
        by_case.setdefault(row["case"], Counter())[row["stage"]] += 1
    total = Counter(row["stage"] for row in rows)
    return {
        "misses": len(rows),
        "total": {s: total[s] for s in STAGES},
        "by_case": {case: {s: c[s] for s in STAGES if c[s]} for case, c in by_case.items()},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--window", type=int, default=WINDOW, help="lines either side of the human comment")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    rows = run(args.window)
    summary = summarize(rows)
    if args.json:
        print(json.dumps({"window": args.window, "summary": summary, "rows": rows}, indent=2))
        return
    for row in rows:
        print(f"[{row['stage']:10}] {row['case']} {Path(row['path']).name}:{row['line']}  human: {row['human']}")
        for c in row["candidates"]:
            tag = c["reason"] or c["reviewers"]
            print(f"             -> line {c['line']} ({tag}): {c['message']}")
    print(f"\nwindow={args.window} misses={summary['misses']} {json.dumps(summary['total'])}")


if __name__ == "__main__":
    main()
