"""F6: does a reviewer cite a line the diff can carry a comment on? Deterministic, no judge, no tokens.

Each raw reviewer finding is classified with the tool's own rule (`anchor_finding` in core/diffmap.py):

  added            the cited line is an added line (what the reviewer prompts ask for)
  context          an unchanged line inside a hunk: the comment attaches, but not to a change
  relocated        not on the diff's right side; moved to the nearest line within 3
  unanchored       in a diffed file but more than 3 lines from any hunk line: summary only
  file_not_in_diff the file is not in the diff (or is binary): summary only

Sources: tests/golden (committed cases, the hand-built `seed-*` case left out) and the local shadow store
(`<run>.diff` with `<run>.raw.json`). Only counts are printed, never finding text.

Usage:
  python -B evals/deepeval/anchoring.py                 # golden and the shadow store
  python -B evals/deepeval/anchoring.py --json out.json # also write the counts
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

from codecheck.core.diffmap import FileDiff, anchor_finding, parse_diff

HERE = Path(__file__).resolve().parent
GOLDEN = HERE.parents[1] / "tests" / "golden"
STORE = Path(os.environ.get("CODE_CHECK_SHADOW_STORE", "~/.local/share/code-check/shadow")).expanduser()
CATEGORIES = ("added", "context", "relocated", "unanchored", "file_not_in_diff")


def classify(files: dict[str, FileDiff], path: str, line: int) -> tuple[str, int | None]:
    """The category of a cited line and how far it moved (None when it did not)."""
    anchor = anchor_finding(files, path, line)
    if anchor is None:
        return ("unanchored" if path in files and not files[path].binary else "file_not_in_diff"), None
    if anchor.relocated:
        return "relocated", abs(anchor.line - line)
    kinds = {ln.new: ln.kind for ln in files[path].lines if ln.new is not None}
    return ("added" if kinds[line] == "add" else "context"), 0


def _relative(path: str, root: str | None) -> str:
    return path[len(root.rstrip("/")) + 1:] if root and path.startswith(root.rstrip("/") + "/") else path


def check_findings(diff_text: str, reviewers: dict, root: str | None = None) -> list[dict]:
    """One row per reviewer finding; findings with no usable file or line are counted as malformed."""
    files = parse_diff(diff_text)
    rows = []
    for reviewer, findings in reviewers.items():
        for f in findings if isinstance(findings, list) else []:
            path, line = (f.get("file"), f.get("line")) if isinstance(f, dict) else (None, None)
            if not isinstance(path, str) or not isinstance(line, int) or isinstance(line, bool):
                rows.append({"reviewer": reviewer, "category": "malformed", "distance": None})
                continue
            category, distance = classify(files, _relative(path, root), line)
            rows.append({"reviewer": reviewer, "category": category, "distance": distance})
    return rows


def summarize(rows: list[dict]) -> dict:
    counts = Counter(r["category"] for r in rows)
    by_reviewer: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        by_reviewer[r["reviewer"]][r["category"]] += 1
    total = len(rows)
    return {
        "total": total,
        "counts": {c: counts.get(c, 0) for c in (*CATEGORIES, "malformed")},
        "share_added": round(counts["added"] / total, 3) if total else None,
        "by_reviewer": {
            name: {"total": sum(c.values()), "added": c["added"], "not_added": sum(c.values()) - c["added"]}
            for name, c in sorted(by_reviewer.items())
        },
    }


def read_json_object(path: Path) -> dict | None:
    """The JSON object in `path`, or None (with a note on stderr) when it is unreadable or not an object.

    The shadow store holds files written by a tool that can be killed mid-write, so one bad file must not stop a scorecard.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:  # ValueError covers JSONDecodeError and UnicodeDecodeError
        print(f"skipping {path.name}: {e}", file=sys.stderr)
        return None
    if not isinstance(data, dict):
        print(f"skipping {path.name}: expected a JSON object", file=sys.stderr)
        return None
    return data


def golden_raws(directory: Path = GOLDEN) -> list[tuple[str, dict]]:
    """(name, case) for each golden case with a diff and reviewer outputs. The hand-built `seed*` case is left out."""
    cases = []
    for path in sorted(directory.glob("*.json")):
        if path.stem.startswith("seed") or (case := read_json_object(path)) is None:
            continue
        if not isinstance(case.get("diff"), str) or not isinstance(case.get("reviewer_outputs"), dict):
            print(f"skipping {path.name}: no diff or reviewer_outputs", file=sys.stderr)
            continue
        cases.append((path.stem, case))
    return cases


def store_raws(directory: Path = STORE) -> list[tuple[str, dict, str]]:
    """(run id, raw outputs, diff text) for each store run that has a diff and a dict of reviewer findings."""
    runs = []
    for raw_path in sorted(directory.glob("*.raw.json")):
        run_id = raw_path.name.removesuffix(".raw.json")
        diff_path = directory / f"{run_id}.diff"
        if not diff_path.is_file() or (raw := read_json_object(raw_path)) is None:
            continue
        if not isinstance(raw.get("reviewers"), dict):
            continue  # a run without reviewer output (an early tool version) has nothing to measure
        try:
            diff = diff_path.read_text()
        except (OSError, ValueError) as e:
            print(f"skipping {diff_path.name}: {e}", file=sys.stderr)
            continue
        runs.append((run_id, raw, diff))
    return runs


def golden_cases(directory: Path = GOLDEN) -> list[tuple[str, str, dict, str | None]]:
    return [(name, case["diff"], case["reviewer_outputs"], None) for name, case in golden_raws(directory)]


def store_runs(directory: Path = STORE) -> list[tuple[str, str, dict, str | None]]:
    return [(run_id, diff, raw["reviewers"], raw.get("root")) for run_id, raw, diff in store_raws(directory)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", help="also write the counts to this file")
    args = ap.parse_args()

    report = {}
    for name, cases in (("golden", golden_cases()), ("shadow_store", store_runs())):
        rows = [r for _, diff, reviewers, root in cases for r in check_findings(diff, reviewers, root)]
        report[name] = {"cases": len(cases), **summarize(rows)}
        s = report[name]
        print(f"{name}: {s['cases']} cases, {s['total']} raw findings, {s['counts']}, share on an added line: {s['share_added']}")
        for reviewer, c in s["by_reviewer"].items():
            print(f"    {reviewer:18} {c['added']}/{c['total']} on an added line")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
