"""Replay golden cases through the deterministic part of a review (validate, merge, anchor, cap, policy).

A golden case stores a real (or hand-built) run's raw reviewer and aggregator outputs with the diff, and the findings
the pipeline posted for it as a human confirmed them. Replaying needs no network and no model, so it runs in
`pytest` and in CI and fails when a code change alters what would be posted for a known run.
"""

import json
from pathlib import Path

from codecheck.commands._common import check_finding_paths, finding_items
from codecheck.core.diffmap import parse_diff
from codecheck.core.findings import finding_id, validate_findings
from codecheck.core.pipeline import finalize_findings, plan_review
from codecheck.core.policy import decide
from codecheck.errors import CodeCheckError

SCHEMA_VERSION = 1
# The plugin repository's own cases; an installed package has no `tests/`, so there this directory is simply missing.
DEFAULT_CASES = Path(__file__).resolve().parents[3] / "tests" / "golden"
REQUIRED = ("version", "case_id", "repo", "pr", "head_sha", "diff", "reviewer_outputs", "aggregator",
            "expected_posted", "expected_decision", "labels", "misses", "signals", "label_status", "repo_private")
_TYPES = {"case_id": str, "diff": str, "reviewer_outputs": dict, "expected_posted": list, "expected_decision": str,
          "labels": list, "misses": list}
# Finding paths are relative to the repository; containment is checked against a root that never exists on disk.
_ROOT = Path("/code-check-golden-replay")


def load_case(path: Path) -> dict:
    path = Path(path)
    try:
        case = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise CodeCheckError(f"golden case {path.name}: not readable JSON: {e}") from e
    if not isinstance(case, dict):
        raise CodeCheckError(f"golden case {path.name}: expected a JSON object")
    missing = [k for k in REQUIRED if k not in case]
    if missing:
        raise CodeCheckError(f"golden case {path.name}: missing required key(s): {', '.join(missing)}")
    if case["version"] != SCHEMA_VERSION:
        raise CodeCheckError(f"golden case {path.name}: unsupported version {case['version']!r}")
    wrong = [k for k, t in _TYPES.items() if not isinstance(case[k], t)]
    if wrong:
        raise CodeCheckError(f"golden case {path.name}: wrong type for: {', '.join(wrong)}")
    if case["label_status"] != "confirmed":
        raise CodeCheckError(f"golden case {path.name}: label_status is {case['label_status']!r}; "
                             "only cases a human confirmed belong in the golden set")
    if not all(isinstance(f, dict) for f in case["expected_posted"]):
        raise CodeCheckError(f"golden case {path.name}: expected_posted must hold objects")
    return case


def load_cases(directory: Path) -> list[dict]:
    directory = Path(directory)
    if not directory.is_dir():
        return []
    return [load_case(p) for p in sorted(directory.glob("*.json"))]


def posted_view(aggregator: object, diff: str, *, auto_approve: bool = False, cap: int = 25) -> dict:
    """What `post` would publish for this aggregator output: each finding in order, inline or summary-only, and the decision."""
    findings, errors = validate_findings(finding_items(aggregator), reviewer="aggregator")
    findings, errors = check_finding_paths(findings, errors, _ROOT, "aggregator")
    if errors:
        raise CodeCheckError("aggregator output invalid: " + "; ".join(errors[:5]))
    diff_files = parse_diff(diff)
    findings = finalize_findings(findings, diff_files)
    plan = plan_review(findings, diff_files, already=set(), cap=cap)
    inline = {finding_id(f) for f, _ in plan.inline}
    decision = decide(findings, auto_approve=auto_approve, is_draft=False)
    return {"findings": [{"file": f.file, "line": f.line, "category": f.category, "severity": f.severity.value,
                          "inline": finding_id(f) in inline} for f in findings],
            "decision": decision.event}


def _name(f: dict) -> str:
    return f"{f.get('file')}:{f.get('line')} [{f.get('category')}]"


def _where(inline) -> str:
    return "inline" if inline else "summary only"


def _key(f: dict) -> tuple:
    return f.get("file"), f.get("line"), f.get("category")


def _compare(actual: list[dict], expected: list[dict]) -> list[str]:
    problems: list[str] = []
    remaining = list(expected)
    for got in actual:
        match = next((e for e in remaining if _key(e) == _key(got)), None)
        if match is None:
            problems.append(f"unexpected finding {_name(got)} ({got['severity']}, {_where(got['inline'])})")
            continue
        remaining.remove(match)
        if match.get("severity") != got["severity"]:
            problems.append(f"{_name(got)}: severity {got['severity']}, expected {match.get('severity')}")
        if match.get("inline") != got["inline"]:
            problems.append(f"{_name(got)}: posted {_where(got['inline'])}, expected {_where(match.get('inline'))}")
    problems.extend(f"expected finding {_name(e)} was not posted" for e in remaining)
    if not problems and actual != expected:
        problems.append("posted findings are in a different order than expected: "
                        + ", ".join(_name(f) for f in actual))
    return problems


def check_case(case: dict) -> list[str]:
    """Every way the pipeline's output for this case differs from what it expects; empty when it replays exactly."""
    problems: list[str] = []
    for member, raw in sorted(case["reviewer_outputs"].items()):
        items = finding_items(raw)
        found, errors = validate_findings(items, reviewer=member)
        total = len(items) if isinstance(items, list) else 0
        if errors or len(found) != total:
            detail = f": {'; '.join(errors[:3])}" if errors else ""
            problems.append(f"reviewer {member}: {len(found)} of {total} stored findings validate{detail}")
    try:
        view = posted_view(case["aggregator"], case["diff"])
    except CodeCheckError as e:
        return [*problems, str(e)]
    problems.extend(_compare(view["findings"], case["expected_posted"]))
    if view["decision"] != case["expected_decision"]:
        problems.append(f"decision {view['decision']}, expected {case['expected_decision']}")
    return problems


def run_replay(directory: Path) -> dict:
    cases = load_cases(directory)
    failed = [{"case": c["case_id"], "problems": p} for c in cases if (p := check_case(c))]
    return {"passed": not failed, "cases": len(cases), "failed": failed}
