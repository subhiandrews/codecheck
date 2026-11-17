"""History of the live and calibration eval scores, kept in the repository so the trend is visible.

`eval --live` and `eval --calibrate` append one line to `docs/eval-history.jsonl` (the record) and rewrite
`docs/eval-history.md` (a Mermaid chart and a table, always generated from the record). Nothing here commits: the
files are changed in the working tree and go in with whatever commit the developer makes next. Recording is best
effort and never changes what an eval reports (see `eval_run._record`).
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from codecheck.vcs.git import Git

# The plugin repository's own docs; an installed package has no `docs/`, and then nothing is recorded.
DEFAULT_DIR = Path(__file__).resolve().parents[3] / "docs"
RECORD = "eval-history.jsonl"
REPORT = "eval-history.md"
MODES = ("live", "calibrate")
CHART_RUNS = 30  # the chart shows the latest runs of a mode, the table the latest TABLE_RUNS
TABLE_RUNS = 15
WINDOW = 3  # trailing runs in the rolling average
LIVE_MODELS = ("aggregator", "language_experts", "reviewers", "deep", "security", "performance")
TITLES = {"live": "Live eval: snippets passed", "calibrate": "Judge calibration: agreement with the human labels"}


def summarize(mode: str, result: dict) -> dict | None:
    """The numbers worth keeping from an eval result, or None when the run measured nothing (skipped, no cases)."""
    if mode == "live":
        cases = result.get("cases") or []
        if result.get("skipped") or not cases:
            return None
        costs = [c["claude"]["cost_usd"] for c in cases if isinstance(c.get("claude", {}).get("cost_usd"), int | float)]
        return {"passed": bool(result["passed"]),
                "cases": {"n": len(cases), "passed": sum(1 for c in cases if c["result"]["passed"]),
                          "failed": [c["name"] for c in cases if not c["result"]["passed"]]},
                "cost_usd": round(sum(costs), 4)}
    agreement = result.get("agreement") or {}
    if not agreement.get("n"):
        return None
    return {"passed": bool(result["passed"]),
            "agreement": {k: agreement.get(k) for k in ("n", "agree", "rate", "kappa", "errors")},
            "cost_usd": result.get("cost_usd")}


def _models(ctx, mode: str) -> dict:
    models = ctx.config.models
    return {"judge": models.judge} if mode == "calibrate" else {k: getattr(models, k) for k in LIVE_MODELS}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def record(ctx, mode: str, result: dict, only: str | None, directory: Path | None = None) -> Path | None:
    """Append this run to the record and regenerate the report. Returns the record path, or None when nothing was
    recorded (an installed package without `docs/`, or a run that measured nothing)."""
    directory = Path(directory) if directory is not None else DEFAULT_DIR
    summary = summarize(mode, result)
    if summary is None or not directory.is_dir():
        return None
    git = Git(ctx.runner, directory.parent)
    entry = {"ts": _now(), "sha": git.head_sha(), "branch": git.current_branch(), "dirty": git.is_dirty(),
             "mode": mode, "partial": only, "models": _models(ctx, mode), **summary}
    path = directory / RECORD
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    write_report(directory)
    return path


def _num(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _usable(entry: object) -> bool:
    """Whether a line has every field the report reads, so one hand-edited or foreign-schema line cannot break it."""
    if not (isinstance(entry, dict) and entry.get("mode") in MODES and isinstance(entry.get("ts"), str)):
        return False
    if entry.get("cost_usd") is not None and not _num(entry["cost_usd"]):
        return False
    if entry.get("models") is not None and not isinstance(entry["models"], dict):
        return False
    if entry["mode"] == "live":
        cases = entry.get("cases")
        failed = cases.get("failed", []) if isinstance(cases, dict) else None
        return (isinstance(cases, dict) and _num(cases.get("n")) and _num(cases.get("passed"))
                and isinstance(failed, list) and all(isinstance(n, str) for n in failed))
    a = entry.get("agreement")
    return (isinstance(a, dict) and _num(a.get("n")) and _num(a.get("agree"))
            and all(a.get(k) is None or _num(a[k]) for k in ("rate", "kappa", "errors")))


def load(path: Path) -> tuple[list[dict], int]:
    """The recorded runs oldest first, and how many lines were unusable. Two branches that each appended a line merge
    in either order (`merge=union`), so the order is restored from the timestamps."""
    entries, bad = [], 0
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return [], 0
    for line in filter(None, (ln.strip() for ln in lines)):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            entry = None
        if _usable(entry):
            entries.append(entry)
        else:
            bad += 1
    entries.sort(key=lambda e: e["ts"])
    return entries, bad


def _score(entry: dict) -> float | None:
    """The run's headline score as a percentage."""
    if entry["mode"] == "live":
        cases = entry.get("cases") or {}
        return 100 * cases["passed"] / cases["n"] if cases.get("n") else None
    rate = (entry.get("agreement") or {}).get("rate")
    return None if rate is None else 100 * rate


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _models_cell(entry: dict) -> str:
    models = entry.get("models") or {}
    if entry["mode"] == "calibrate":
        return _cell(f"judge {models.get('judge', '?')}")
    return _cell(f"reviewers {models.get('reviewers', '?')}, aggregator {models.get('aggregator', '?')}")


def _row(number: int, entry: dict) -> str:
    score = _score(entry)
    if entry["mode"] == "live":
        cases = entry["cases"]
        outcome = f"{cases['passed']}/{cases['n']}" + (f" (failed: {', '.join(cases['failed'])})" if cases.get("failed") else "")
    else:
        a = entry["agreement"]
        kappa = "n/a" if a.get("kappa") is None else f"{a['kappa']:.2f}"
        outcome = f"{a['agree']}/{a['n']}, kappa {kappa}" + (f", {a['errors']} unusable" if a.get("errors") else "")
    cost = entry.get("cost_usd")
    sha = f"{str(entry.get('sha', ''))[:7]}{'+dirty' if entry.get('dirty') else ''}"
    return (f"| {number} | {entry['ts'][:10]} | {_cell(sha)} | {_cell(entry.get('branch', ''))} | "
            f"{'n/a' if score is None else f'{score:.0f}%'} | {_cell(outcome)} | {_models_cell(entry)} | "
            f"{'n/a' if cost is None else f'${cost:.2f}'} |")


def _rolling(values: list[float]) -> list[float]:
    windows = [values[max(0, i - WINDOW + 1): i + 1] for i in range(len(values))]
    return [sum(w) / len(w) for w in windows]


def _full_runs(entries: list[dict], mode: str) -> list[dict]:
    return [e for e in entries if e["mode"] == mode and not e.get("partial") and _score(e) is not None]


def _section(mode: str, entries: list[dict]) -> list[str]:
    full = _full_runs(entries, mode)
    out = [f"## {TITLES[mode]}", ""]
    if not full:
        return [*out, "No runs recorded yet.", ""]
    numbered = list(enumerate(full, start=1))
    shown = numbered[-CHART_RUNS:]
    scores = [_score(e) for _, e in shown]
    out += ["```mermaid", "xychart-beta", f'    title "{TITLES[mode]} (%), rolling average of {WINDOW} runs"',
            f"    x-axis [{', '.join(str(n) for n, _ in shown)}]", '    y-axis "percent" 0 --> 100',
            f"    line [{', '.join(f'{s:.0f}' for s in scores)}]",
            f"    line [{', '.join(f'{s:.0f}' for s in _rolling(scores))}]", "```", "",
            "| # | date | commit | branch | score | outcome | models | cost |", "|---|---|---|---|---|---|---|---|"]
    out += [_row(n, e) for n, e in reversed(numbered[-TABLE_RUNS:])]
    return [*out, ""]


def render(entries: list[dict], bad: int = 0) -> str:
    lines = ["# Eval history", "",
             "Generated by `code-check eval --history` from `eval-history.jsonl`; do not edit by hand. Each full",
             "`eval --live` or `eval --calibrate` run appends one line to that file and rewrites this one, and the",
             "developer commits both. Runs limited with `--only` are kept in the file but left out here. Live",
             "results vary from run to run, so read the rolling line (the second one) and not a single point, and",
             "check the models column before putting a change down to a prompt. The judge score measures the judge",
             "against the human labels, not the reviewers: it moves when the judge or the labels change.", ""]
    for mode in MODES:
        lines += _section(mode, entries)
    if bad:
        lines += [f"{bad} unusable line(s) in `eval-history.jsonl` were ignored.", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def write_report(directory: Path) -> Path:
    directory = Path(directory)
    entries, bad = load(directory / RECORD)
    report = directory / REPORT
    tmp = report.with_name(report.name + ".tmp")
    tmp.write_text(render(entries, bad), encoding="utf-8")
    os.replace(tmp, report)
    return report


def regenerate(directory: Path | None = None) -> dict:
    """`eval --history`: rebuild the report from the record and say what the latest runs were."""
    directory = Path(directory) if directory is not None else DEFAULT_DIR
    if not directory.is_dir():
        return {"passed": True, "runs": {m: 0 for m in MODES}, "report": None}
    entries, bad = load(directory / RECORD)
    report = write_report(directory)
    out = {"passed": True, "report": str(report), "unusable_lines": bad,
           "runs": {m: sum(1 for e in entries if e["mode"] == m) for m in MODES}}
    for mode in MODES:
        full = _full_runs(entries, mode)
        if full:
            out[mode] = {"latest": _score(full[-1]), "previous": _score(full[-2]) if len(full) > 1 else None}
    return out
