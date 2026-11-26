"""Compare two arms of a PR replay without mistaking sampling noise for an effect (no model, no tokens).

One review of the same prompts finds a different share of a PR's labelled comments each time (8 to 14 of 20 on one
case), so one run per arm cannot say which arm is better. An arm is better on a case only when every one of its runs
beats every run of the other arm, and only when both arms have `min_runs` or more runs."""

import json
from pathlib import Path

from codecheck.errors import CodeCheckError

MIN_RUNS = 3


def load_results(path: Path) -> list[dict]:
    """The `--out` file of a replay `run`: one JSON object per line."""
    try:
        lines = Path(path).expanduser().read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as e:
        raise CodeCheckError(f"results file {path}: not readable: {e}") from e
    results = []
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise CodeCheckError(f"results file {path}: line {n} is not JSON: {e}") from e
        if not isinstance(row, dict):
            raise CodeCheckError(f"results file {path}: line {n} is not a JSON object")
        results.append(row)
    return results


def _recall(result: dict) -> float | None:
    score = result.get("score")
    recall = score.get("recall") if isinstance(score, dict) else None
    return float(recall) if isinstance(recall, int | float) and not isinstance(recall, bool) else None


def _by_case(results: list[dict]) -> dict[str, dict]:
    cases: dict[str, dict] = {}
    for r in results:
        name = r.get("name")
        if not isinstance(name, str):
            continue
        entry = cases.setdefault(name, {"recalls": [], "skipped": 0})
        recall = None if r.get("error") else _recall(r)
        if recall is None:  # a failed review, a dry run, or a case with no labelled comment in the diff
            entry["skipped"] += 1
        else:
            entry["recalls"].append(recall)
    return cases


def _stats(entry: dict) -> dict:
    recalls = entry["recalls"]
    stats = {"runs": len(recalls), "skipped": entry["skipped"]}
    if recalls:
        stats |= {"mean": sum(recalls) / len(recalls), "min": min(recalls), "max": max(recalls)}
    return stats


def _verdict(a: dict, b: dict, min_runs: int) -> str:
    if a["runs"] < min_runs or b["runs"] < min_runs:
        return "too-few-runs"
    if a["min"] > b["max"]:
        return "a-better"
    if b["min"] > a["max"]:
        return "b-better"
    return "within-noise"


def compare_arms(a_results: list[dict], b_results: list[dict], *, min_runs: int = MIN_RUNS) -> dict:
    """Per case, recall mean, min and max of each arm and whether one arm clearly beats the other; then one overall
    verdict: `a-better` or `b-better` when only that arm wins cases, `mixed` when each wins some, `too-few-runs` when
    no case has enough runs in both arms to say, else `within-noise`."""
    a_cases, b_cases = _by_case(a_results), _by_case(b_results)
    rows = []
    for name in sorted(a_cases.keys() | b_cases.keys()):
        if name not in a_cases or name not in b_cases:
            rows.append({"name": name, "verdict": "missing-arm", "in": "a" if name in a_cases else "b"})
            continue
        a, b = _stats(a_cases[name]), _stats(b_cases[name])
        rows.append({"name": name, "a": a, "b": b, "verdict": _verdict(a, b, min_runs)})
    verdicts = [r["verdict"] for r in rows]
    a_wins, b_wins = verdicts.count("a-better"), verdicts.count("b-better")
    if a_wins and b_wins:
        overall = "mixed"
    elif a_wins or b_wins:
        overall = "a-better" if a_wins else "b-better"
    elif "within-noise" in verdicts:
        overall = "within-noise"
    else:
        overall = "too-few-runs"
    return {"verdict": overall, "min_runs": min_runs, "a_better": a_wins, "b_better": b_wins,
            "within_noise": verdicts.count("within-noise"), "too_few_runs": verdicts.count("too-few-runs"),
            "missing_arm": verdicts.count("missing-arm"), "cases": rows}
