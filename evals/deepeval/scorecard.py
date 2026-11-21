"""Scorecard: run the deterministic metrics (metrics.py) over every run in the shadow store or the golden cases.

Each run becomes a DeepEval `LLMTestCase` whose `metadata` carries the diff, the raw reviewer findings, what the
aggregator kept and dropped, and which reviewers wrote a findings file; the cases go into an `EvaluationDataset`.
Nothing here calls a model, and only counts, run ids and metric reasons are printed or written, never finding text.

The golden diffs are also among the store runs, so the two sources are separate: pick one, never add them.

Usage:
  python -B evals/deepeval/scorecard.py                    # the shadow store (default)
  python -B evals/deepeval/scorecard.py --source golden
  python -B evals/deepeval/scorecard.py --misses --json out.json
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import _env  # noqa: F401  (telemetry opt-out, must run before deepeval is imported)
from anchoring import GOLDEN, STORE, golden_raws, read_json_object, store_raws
from deepeval.dataset import EvaluationDataset
from deepeval.test_case import LLMTestCase
from metrics import default_metrics


def _case(name: str, meta: dict) -> LLMTestCase:
    return LLMTestCase(input=name, actual_output=f"{len(meta['kept'])} kept findings", name=name, metadata=meta)


def _aggregated(raw: dict) -> tuple[list, list]:
    """The aggregator's kept and dropped lists, or empty lists when the output is missing or oddly shaped."""
    aggregator = raw.get("aggregator")
    aggregator = aggregator if isinstance(aggregator, dict) else {}
    kept, dropped = aggregator.get("findings"), aggregator.get("dropped")
    return (kept if isinstance(kept, list) else []), (dropped if isinstance(dropped, list) else [])


def golden_cases(directory: Path = GOLDEN) -> list[LLMTestCase]:
    cases = []
    for name, case in golden_raws(directory):
        kept, dropped = _aggregated(case)
        cases.append(_case(f"golden:{name}", {
            "source": "golden", "diff": case["diff"], "root": None, "reviewers": case["reviewer_outputs"],
            "kept": kept, "dropped": dropped, "reviewer_output": None,
        }))
    return cases


def store_cases(directory: Path = STORE) -> list[LLMTestCase]:
    cases = []
    for run_id, raw, diff in store_raws(directory):
        handoff_path = directory / f"{run_id}.handoff" / "handoff.json"
        handoff = (read_json_object(handoff_path) if handoff_path.is_file() else None) or {}
        reviewers = handoff.get("reviewers")
        status = {name: bool(v.get("output")) for name, v in (reviewers if isinstance(reviewers, dict) else {}).items()
                  if isinstance(v, dict)}
        kept, dropped = _aggregated(raw)
        cases.append(_case(f"store:{run_id}", {
            "source": "store", "run_id": run_id, "tool_version": handoff.get("tool_version"), "diff": diff,
            "root": raw.get("root"), "reviewers": raw["reviewers"], "kept": kept, "dropped": dropped,
            "reviewer_output": status or None,
        }))
    return cases


def build_dataset(cases: list[LLMTestCase]) -> EvaluationDataset:
    dataset = EvaluationDataset()
    for case in cases:
        dataset.add_test_case(case)
    return dataset


def score(dataset: EvaluationDataset) -> list[dict]:
    """One row per (run, metric) where the metric applies to the run."""
    rows = []
    for case in dataset.test_cases:
        for metric in default_metrics():  # fresh instances: a metric keeps its last score on itself
            if metric.applies(case.metadata or {}):
                metric.measure(case)
                rows.append({"case": case.name, "metric": metric.name, "score": metric.score, "n": metric.numerator,
                             "of": metric.denominator, "success": metric.success, "reason": metric.reason,
                             "version": (case.metadata or {}).get("tool_version")})
    return rows


def by_version(rows: list[dict]) -> dict[str, list[dict]]:
    """The summary per tool version (runs with no recorded version are grouped as "unknown")."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row.get("version") or "unknown"].append(row)
    return {version: summarize(group) for version, group in sorted(groups.items())}


def summarize(rows: list[dict]) -> list[dict]:
    by_metric: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_metric[row["metric"]].append(row)
    out = []
    for name, group in by_metric.items():
        n, of = sum(r["n"] for r in group), sum(r["of"] for r in group)
        with_target = [r for r in group if r["success"] is not None]
        out.append({
            "metric": name, "runs": len(group), "pooled": n, "of": of, "pooled_share": round(n / of, 3) if of else None,
            "runs_at_target": sum(bool(r["success"]) for r in with_target) if with_target else None,
            "runs_with_target": len(with_target),
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["store", "golden"], default="store")
    ap.add_argument("--misses", action="store_true", help="also list each run that misses a metric's target")
    ap.add_argument("--by-version", action="store_true", help="also break each metric down by tool version")
    ap.add_argument("--json", help="also write the rows and summary to this file")
    args = ap.parse_args()

    dataset = build_dataset(store_cases() if args.source == "store" else golden_cases())
    rows = score(dataset)
    summary = summarize(rows)
    print(f"{args.source}: {len(dataset.test_cases)} runs")
    for s in summary:
        target = "no target" if s["runs_with_target"] == 0 else f"{s['runs_at_target']}/{s['runs_with_target']} runs at target"
        print(f"  {s['metric']:56} {s['pooled']:>4}/{s['of']:<4} = {s['pooled_share']:.3f}   over {s['runs']:>2} runs, {target}")
    if args.by_version:
        for version, group in by_version(rows).items():
            print(f"  tool {version}")
            for s in group:
                print(f"      {s['metric']:56} {s['pooled']:>4}/{s['of']:<4} = {s['pooled_share']:.3f}   ({s['runs']} runs)")
    if args.misses:
        for r in rows:
            if r["success"] is False:
                print(f"  miss: {r['case']}  {r['metric']}  {r['n']}/{r['of']}  ({r['reason']})")
    if args.json:
        Path(args.json).write_text(json.dumps({"source": args.source, "summary": summary, "rows": rows}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
