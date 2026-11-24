"""Evidence for improving a CODE_CHECK.md, and the checks a proposed new text must pass. No model, no clock; only `gather` reads the shadow store.

The evidence is what the shadow store learned about past reviews of one repository: findings the author's own
verdicts called incorrect, and human review comments the panel did not match to any finding. It only says where the
guidance could be better; a model writes the rules, and `check_proposal` limits what that text may do.
"""

import difflib
import hashlib
import json
from collections.abc import Mapping

from codecheck.shadow.export import finding_label, run_feedback
from codecheck.shadow.scrub import scrub, scrub_text
from codecheck.shadow.store import ShadowStore

MIN_PRS = 2  # one PR is one person's opinion: a pattern needs this many distinct PRs
MAX_CATEGORIES = 6
MAX_EXAMPLES = 3
MAX_MISSES = 12
TEXT_CAP = 300
MAX_FILE_CHARS = 8_000  # a written file; the reviewers read all guidance files within 16,000 characters together
MAX_CHANGED_LINES = 40  # lines added plus lines removed by one proposal


def _under(path: object, directory: str) -> bool:
    return isinstance(path, str) and (not directory or path.startswith(f"{directory}/"))


def _clip(text: object) -> str:
    return scrub_text(" ".join(str(text or "").split()))[:TEXT_CAP]


def gather(shadow: ShadowStore, repo: str, directory: str) -> dict:
    """What the store says about `repo` for files under `directory` ("" is the whole repository).

    `disputed` lists finding categories whose findings were disputed in at least `MIN_PRS` PRs and in more PRs than
    they were confirmed in. `misses` are human comments no finding matched, kept when they come from at least
    `MIN_PRS` PRs. `proposable` says there is enough of either to be worth a proposal, and `digest` identifies exactly
    this evidence."""
    cats: dict[str, dict] = {}
    misses: list[dict] = []
    miss_prs: set[str] = set()
    pr_runs = labelled = 0
    for rid, (outcomes, humans) in run_feedback(shadow).items():
        recs = shadow.read(rid)
        run = next((r for r in recs if r.get("type") == "run"), None)
        if run is None or run.get("repo") != repo or not isinstance(run.get("pr"), str):
            continue
        pr = run["pr"]
        pr_runs += 1
        for f in (r for r in recs if r.get("type") == "finding" and isinstance(r.get("id"), str)):
            if not _under(f.get("file"), directory):
                continue
            label = finding_label(f["id"], outcomes, humans)
            if label not in ("confirmed", "disputed"):
                continue
            labelled += 1
            entry = cats.setdefault(str(f.get("category") or "other"), {"confirmed": set(), "disputed": set(), "examples": []})
            entry[label].add(pr)
            if label == "disputed" and len(entry["examples"]) < MAX_EXAMPLES:
                entry["examples"].append({"file": f.get("file"), "severity": f.get("severity"), "message": _clip(f.get("message"))})
        for h in humans.values():
            if not h.get("matches_finding") and _under(h.get("path"), directory):
                miss_prs.add(pr)
                if len(misses) < MAX_MISSES:
                    misses.append({"pr": pr, "path": h.get("path"), "body": _clip(h.get("body"))})
    disputed = [
        {"category": cat, "prs": len(e["disputed"]), "confirmed_prs": len(e["confirmed"]), "examples": e["examples"]}
        for cat, e in sorted(cats.items())
        if len(e["disputed"]) >= MIN_PRS and len(e["disputed"]) > len(e["confirmed"])
    ][:MAX_CATEGORIES]
    out = {"disputed": disputed, "misses": misses if len(miss_prs) >= MIN_PRS else [], "miss_prs": len(miss_prs)}
    out = scrub(out)
    out["proposable"] = bool(out["disputed"] or out["misses"])
    out["digest"] = hashlib.sha256(json.dumps(out, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    if not out["proposable"]:  # after the digest: the reason explains the evidence, it is not part of it
        contested = any(len(e["disputed"]) >= MIN_PRS and len(e["disputed"]) <= len(e["confirmed"]) for e in cats.values())
        out["reason"] = _why_empty(pr_runs, labelled, len(miss_prs), contested)
    return out


def _why_empty(pr_runs: int, labelled: int, miss_prs: int, contested: bool) -> str:
    """`contested`: some category was disputed in at least MIN_PRS PRs but confirmed in as many."""
    if not pr_runs:
        return ("no review-pr runs of this repository are recorded here; local reviews and address-comments runs "
                "carry no evidence, which comes from reviews posted to a PR and the feedback they received")
    parts = []
    if not labelled:
        parts.append(f"{pr_runs} recorded PR runs, but none has a finding that an author or reviewer has responded to yet")
    elif contested:
        parts.append("a category was disputed in several PRs but confirmed in as many, so it is not a pattern")
    else:
        parts.append(f"no finding category was disputed in {MIN_PRS} or more PRs")
    if miss_prs:
        parts.append(f"missed human comments come from {miss_prs} PR (a pattern needs {MIN_PRS})")
    return "; ".join(parts)


def changed_lines(old: str | None, new: str) -> int:
    before = (old or "").splitlines()
    return sum(1 for line in difflib.ndiff(before, new.splitlines()) if line[:2] in ("+ ", "- "))


def check_proposal(old: str | None, new: str) -> str | None:
    """Why `new` may not replace `old` as a CODE_CHECK.md, or None when it may."""
    if not new.strip():
        return "the proposed text is empty"
    if len(new) > MAX_FILE_CHARS:
        return f"the proposed text is {len(new)} characters; a CODE_CHECK.md written here may hold at most {MAX_FILE_CHARS}"
    if scrub_text(new) != new:
        return "the proposed text contains something that looks like a secret"
    if old is not None and new.strip() == old.strip():
        return "the proposed text does not change the file"
    n = changed_lines(old, new)
    if n > MAX_CHANGED_LINES:
        return f"the proposal changes {n} lines; propose at most {MAX_CHANGED_LINES} changed lines at a time"
    return None


def diff_text(old: str | None, new: str, name: str) -> str:
    before = "/dev/null" if old is None else f"a/{name}"  # a new file, so that `git apply` creates it
    lines = difflib.unified_diff((old or "").splitlines(True), new.splitlines(True), before, f"b/{name}")
    # difflib never says when a side lacks its final newline, which glues the next line onto it and corrupts the patch
    return "".join(line if line.endswith("\n") else f"{line}\n\\ No newline at end of file\n" for line in lines)


def summary(evidence: Mapping) -> str:
    return f"{len(evidence['disputed'])} disputed categories, {len(evidence['misses'])} missed comments"
