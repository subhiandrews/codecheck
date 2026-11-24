"""Step 3: a code-grounded validity judge. Is a finding's claim true of the code at the PR head commit?

A GEval metric (same pattern as same_concern.py) sees the finding as `input` and the code as `retrieval_context`:
the cited file (whole when it is at most FILE_CAP characters, else a window around the cited line), and the PR diff
with the cited file's section first. It never sees a label,
a checker verdict or the aggregator's reason. The judge scores 0-10 against fixed steps, DeepEval turns that into
0-1, and `label_for` maps it to TRUE, FALSE or UNCERTAIN.

The pass bar was fixed BEFORE any run and agreed with the owner on 2026-11-21, so it cannot be tuned afterwards:
ground truth is the items that both independent checker passes called TRUE or FALSE, less the three in EXCLUDED
(9 items: 4 TRUE, 5 FALSE); the judge passes at BAR_CORRECT of 9 correct including BAR_TRUE_FOUND of the 4 TRUE (an
always-FALSE judge scores 5 of 9). UNCERTAIN counts as wrong. The run happens once; a second run is refused unless
--rerun, and a rerun is exploratory. History: the first bar was 10 of 12; it was rescaled to 8 of 9 after a check
showed the judge's context could not hold the deciding code for 3 items, which made 10 of 12 unreachable.

Known limit: the judge gets one static context. A checker could search the whole repo; the judge cannot, so a claim
whose deciding code is in a file that is neither the cited file nor in the diff may come back UNCERTAIN. The three
items where that is known to hold are excluded; others may exist, so read any UNCERTAIN result with that in mind.

Usage (the run costs tokens and needs ANTHROPIC_API_KEY in that command's environment only):
  python -B evals/deepeval/code_validity.py          # dry run: builds the contexts, prints sizes, calls nothing
  python -B evals/deepeval/code_validity.py --yes    # the one pre-registered run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import _env  # noqa: F401  (telemetry opt-out, must run before deepeval is imported)

EVALS = Path("~/.local/share/code-check-evals").expanduser()
TASKS = EVALS / "code-check"
AGREEMENT = EVALS / "code-check-pass2" / "agreement.json"
RUNS = EVALS / "judge-runs"
FIRST_RUN = RUNS / "code_validity_run1.json"

TRUE_AT = 0.9
FALSE_BELOW = 0.4
BAR_CORRECT = 8
BAR_TRUE_FOUND = 3  # implied by 8 of 9 (at most one miss), kept so the bar reads as agreed
TRUTH_SIZE = 9
# Left out of the truth set BEFORE any run, by the owner's decision on 2026-11-21, because the code that decides
# them is in neither the cited file nor the PR diff, so a judge with this context cannot see it: the Guava test
# helper that asserts canDecode lives in BaseEncodingTest.java (the finding cites BaseEncoding.java), and the Tokio
# BlockingPool join lives in blocking/pool.rs. Both checker passes called all three FALSE.
EXCLUDED = {"2448c039ebb0:d1", "84048732c6dd:d0", "84048732c6dd:d1"}
FILE_CAP = 60_000  # characters: the whole cited file when it fits, otherwise a window of FILE_RADIUS lines each side
FILE_RADIUS = 300
DIFF_CAP = 40_000

STEPS = [
    (
        "Read the review finding (input) and the code (retrieval context): the cited file, "
        "and the pull request diff. Ignore severity, tone, and whether the finding is worth posting."
    ),
    (
        "List the factual statements the finding makes about the code: what the code does, what a test asserts "
        "or does not assert, what a comment or doc says, which lines or functions exist. A request or suggestion "
        "is not a factual statement."
    ),
    (
        "For each statement, find the code in the context that confirms or contradicts it. A statement about a "
        "library, tool or service whose code is not in the context is not confirmed. Do not use outside "
        "knowledge to fill a gap in the context."
    ),
    (
        "Score 9-10 if the central claim and every stated detail are confirmed by the code in the context. "
        "Score 0-3 if the code in the context contradicts the central claim. Score 4-8 if part of it holds, a "
        "stated consequence is not confirmed, or the context does not settle it."
    ),
]


def label_for(score: float) -> str:
    if score >= TRUE_AT:
        return "TRUE"
    return "UNCERTAIN" if score >= FALSE_BELOW else "FALSE"


def bar(predicted: dict[str, str], truth: dict[str, str]) -> dict:
    """Score a run against the truth set. A missing or UNCERTAIN prediction counts as wrong."""
    correct = sum(predicted.get(k) == v for k, v in truth.items())
    true_total = sum(v == "TRUE" for v in truth.values())
    true_found = sum(v == "TRUE" and predicted.get(k) == "TRUE" for k, v in truth.items())
    return {
        "correct": correct, "of": len(truth), "true_found": true_found, "true_total": true_total,
        "wrong": sorted(k for k, v in truth.items() if predicted.get(k) != v),
        "passes": len(truth) == TRUTH_SIZE and correct >= BAR_CORRECT and true_found >= BAR_TRUE_FOUND,
    }


def _bounds(total: int, line: int, radius: int) -> tuple[int, int]:
    center = min(max(line, 1), max(total, 1))
    return max(1, center - radius), min(total, center + radius)


def numbered_window(text: str, line: int, radius: int = FILE_RADIUS) -> str:
    """Numbered lines around `line` (marked with >), clipped to the file."""
    lines = text.splitlines()
    start, end = _bounds(len(lines), line, radius)
    return "\n".join(f"{'>' if i == line else ' '}{i:>3}| {lines[i - 1]}" for i in range(start, end + 1))


def diff_sections(diff: str, first_file: str, cap: int = DIFF_CAP) -> str:
    """The diff by file section: the cited file's section first (whole), then the rest while they fit under `cap`."""
    sections = [s for s in re.split(r"(?m)^(?=diff --git )", diff) if s.strip()]
    cited = [s for s in sections if f"a/{first_file} " in s.splitlines()[0]]
    rest = [s for s in sections if s not in cited]
    out, used, hidden = list(cited), sum(len(s) for s in cited), 0
    for section in rest:
        if used + len(section) <= cap:
            out.append(section)
            used += len(section)
        else:
            hidden += 1
    text = "".join(out)
    return text + (f"\n[{hidden} more file sections of the diff are not shown: size cap reached]\n" if hidden else "")


def safe_ref(commit: str, path: str) -> str:
    """`<commit>:<path>` for git, refusing anything that could act as an option or leave the repository."""
    parts = path.split("/")
    if not re.fullmatch(r"[0-9a-f]{7,40}", commit) or path.startswith(("-", "/")) or ".." in parts or "\0" in path:
        raise ValueError(f"unsafe git reference: {commit!r}:{path!r}")
    return f"{commit}:{path}"


def _git_show(clone: str, ref: str) -> str:
    done = subprocess.run(["git", "-C", clone, "show", ref], capture_output=True, text=True, check=True)  # noqa: S603, S607  (argv list, ref validated by safe_ref)
    return done.stdout


def build_context(task: dict, *, git_show=_git_show, file_cap: int = FILE_CAP, file_radius: int = FILE_RADIUS) -> list[str]:
    """Two retrieval-context parts for one task: the cited file, then the PR diff.

    The file is given whole when it is at most `file_cap` characters, otherwise as a window of `file_radius` lines
    each side of the cited line; the header says which."""
    ref = safe_ref(task["commit"], task["file"])
    short = task["commit"][:10]
    try:
        text = git_show(task["clone"], ref)
        total = len(text.splitlines())
        if len(text) <= file_cap:
            scope, radius = f"whole file, {total} lines", total
        else:
            start, end = _bounds(total, task["line"], file_radius)
            scope, radius = f"lines {start}-{end} of {total}", file_radius
        file_part = (f"File {task['file']} at commit {short} ({scope}; the cited line {task['line']} is marked >):\n"
                     f"{numbered_window(text, task['line'], radius)}")
    except (FileNotFoundError, subprocess.CalledProcessError):
        file_part = f"File {task['file']} does not exist at commit {short}; it may be added only in the diff below."
    diff = diff_sections(Path(task["diff_file"]).read_text(), task["file"])
    return [file_part, f"Pull request diff (the cited file's section comes first):\n{diff}"]


def load_truth(path: Path = AGREEMENT) -> dict[str, str]:
    """Items both checker passes called TRUE or FALSE, less EXCLUDED."""
    agreed = json.loads(Path(path).read_text())["agreed"]
    return {k: v for k, v in agreed.items() if v in ("TRUE", "FALSE") and k not in EXCLUDED}


def load_tasks(directory: Path = TASKS) -> dict[str, dict]:
    tasks: dict[str, dict] = {}
    for path in sorted(directory.glob("tasks-*.json")):
        for task in json.loads(path.read_text()):
            tasks[task["id"]] = task
    return tasks


def build_metric(judge):
    from deepeval.metrics import GEval
    from deepeval.test_case import SingleTurnParams

    return GEval(
        name="Claim true of the code",
        evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.RETRIEVAL_CONTEXT],
        evaluation_steps=STEPS,
        model=judge,
        async_mode=False,
    )


def run(truth: dict[str, str], tasks: dict[str, dict], judge) -> list[dict]:
    from deepeval.test_case import LLMTestCase

    metric = build_metric(judge)
    rows = []
    for item_id in sorted(truth):
        task = tasks[item_id]
        case = LLMTestCase(input=task["claim"], actual_output="", retrieval_context=build_context(task))
        metric.measure(case)
        rows.append({"id": item_id, "truth": truth[item_id], "score": metric.score, "predicted": label_for(metric.score),
                     "reason": metric.reason})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="actually call the judge (the one pre-registered run)")
    ap.add_argument("--rerun", action="store_true", help="allow a second run; it is exploratory and changes no verdict")
    args = ap.parse_args()

    truth, tasks = load_truth(), load_tasks()
    missing = sorted(set(truth) - set(tasks))
    if missing or len(truth) != TRUTH_SIZE:
        print(f"expected {TRUTH_SIZE} truth items with tasks; got {len(truth)}, missing tasks for {missing}", file=sys.stderr)
        return 2

    from judge import DEFAULT_JUDGE_MODEL

    model = os.environ.get("DEEPEVAL_JUDGE_MODEL", DEFAULT_JUDGE_MODEL)
    sizes = {k: [len(p) for p in build_context(tasks[k])] for k in truth}
    chars = sum(len(tasks[k]["claim"]) + sum(sizes[k]) for k in truth)
    print(f"{len(truth)} items ({sum(v == 'TRUE' for v in truth.values())} TRUE, {sum(v == 'FALSE' for v in truth.values())} FALSE), "
          f"{len(truth)} judge calls to {model}; about {chars // 4:,} input tokens in total (characters / 4, plus the rubric)")
    for k in sorted(truth):
        print(f"  {k:18} file window {sizes[k][0]:>6} chars, diff {sizes[k][1]:>6} chars")
    if not args.yes:
        print("dry run: pass --yes to call the judge")
        return 0
    if FIRST_RUN.exists() and not args.rerun:
        print(f"the pre-registered run already exists ({FIRST_RUN}); pass --rerun for an exploratory rerun", file=sys.stderr)
        return 3

    from judge import AnthropicJudge

    rows = run(truth, tasks, AnthropicJudge())
    result = bar({r["id"]: r["predicted"] for r in rows}, truth)
    RUNS.mkdir(parents=True, exist_ok=True)
    path = FIRST_RUN if not FIRST_RUN.exists() else RUNS / f"code_validity_rerun_{int(time.time())}.json"
    path.write_text(json.dumps({"model": model, "bar": result, "rows": rows}, indent=2))
    for r in rows:
        print(f"{r['id']:18} truth {r['truth']:5} score {r['score']:.2f} -> {r['predicted']}")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
