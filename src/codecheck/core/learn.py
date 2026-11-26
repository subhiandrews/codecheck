"""Evidence for improving a CODE_CHECK.md, and the checks a proposed new text must pass. No model, no clock; only `gather` reads the shadow store.

The evidence is what the shadow store learned about past reviews of one repository: findings the author's own
verdicts called incorrect or only a preference, and human review comments the panel did not match to any finding. It only says where the
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


def _contested(e: dict) -> bool:
    """A category disputed in at least MIN_PRS PRs and confirmed in at least as many: not a pattern."""
    return len(e["disputed"]) >= MIN_PRS and len(e["disputed"]) <= len(e["confirmed"])


def _under(path: object, directory: str) -> bool:
    return isinstance(path, str) and (not directory or path.startswith(f"{directory}/"))


def _clip(text: object) -> str:
    return scrub_text(" ".join(str(text or "").split()))[:TEXT_CAP]


def gather(shadow: ShadowStore, repo: str, directory: str) -> dict:
    """What the store says about `repo` for files under `directory` ("" is the whole repository).

    `disputed` lists finding categories whose findings were disputed (verdict `incorrect` or `preference`) in at least `MIN_PRS` PRs and in more PRs than
    they were confirmed in. `misses` are human comments no finding matched, kept when they come from at least
    `MIN_PRS` PRs. `proposable` says there is enough of either to be worth a proposal, and `digest` identifies exactly
    this evidence. When not proposable, `reason` says why; it is not part of the digest."""
    cats: dict[str, dict] = {}
    misses: list[dict] = []
    miss_prs: set[str] = set()
    address_prs: set[str] = set()
    seen: dict[tuple[str, str], tuple[str, str]] = {}  # (pr, finding id) -> (category, label): one count per finding
    reviews = labelled = replied = 0  # review runs; findings with a verdict; findings with only a reply
    for rid, (outcomes, humans) in run_feedback(shadow).items():
        recs = shadow.read(rid)
        run = next((r for r in recs if r.get("type") == "run"), None)
        if run is None or run.get("repo") != repo or not isinstance(run.get("pr"), str):
            continue
        pr = run["pr"]
        # an address-comments run carries a pr too, but a review of ours only when it found findings in the threads
        if run.get("skill") != "address-comments":
            reviews += 1
        elif pr not in address_prs and any(r.get("type") == "finding" for r in recs):
            address_prs.add(pr)  # each `threads` call is a run: count the PR once
            reviews += 1
        for f in (r for r in recs if r.get("type") == "finding" and isinstance(r.get("id"), str)):
            if not _under(f.get("file"), directory):
                continue
            label = finding_label(f["id"], outcomes, humans)
            if (
                (outcomes.get(f["id"]) or {}).get("verdict") == "preference"
                and f.get("category") != "security"  # `preference` is also how big or late Suggestions are declined: never learn to stop raising security
                and not any(h.get("matches_finding") == f["id"] for h in humans.values())
            ):
                label = "disputed"  # declined as taste, and no person raised it: guidance can say when not to raise it. Only this evidence changes; the golden export labels as before
            key = (pr, f["id"])  # runs are chronological: a later decided label replaces an earlier one
            if key not in seen or label in ("confirmed", "disputed"):
                seen[key] = (_clip(f.get("category")) or "other", label)  # clipped: stats is added after the scrub
            replied += label == "replied"
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
        if len(e["disputed"]) >= MIN_PRS and not _contested(e)
    ][:MAX_CATEGORIES]
    out = {"disputed": disputed, "misses": misses if len(miss_prs) >= MIN_PRS else [], "miss_prs": len(miss_prs)}
    out = scrub(out)
    out["proposable"] = bool(out["disputed"] or out["misses"])
    stats = _stats(reviews, seen)  # a baseline for judging a rule later; not evidence, so not in the digest
    out["digest"] = hashlib.sha256(json.dumps(out, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    out["stats"] = stats
    if not out["proposable"]:  # after the digest: the reason explains the evidence, it is not part of it
        contested = any(_contested(e) for e in cats.values())
        out["reason"] = _why_empty(reviews, labelled, replied, len(miss_prs), contested, bool(directory))
    return out


def _stats(reviews: int, seen: dict[tuple[str, str], tuple[str, str]]) -> dict:
    """Findings by what became of them: `dispute_rate` is disputed over decided (confirmed or disputed), None when
    nothing is decided. `reviews` counts every review of the repository, while the findings follow `directory`, and
    `categories_truncated` says `by_category` was cut. Compare `dispute_rate` before and after a learned rule is merged."""
    def block(labels: list[str]) -> dict:
        counts = {k: labels.count(k) for k in ("confirmed", "disputed", "replied")}
        decided = counts["confirmed"] + counts["disputed"]
        return {"findings": len(labels), **counts, "no_signal": len(labels) - sum(counts.values()),
                "dispute_rate": round(counts["disputed"] / decided, 2) if decided else None}
    by_cat: dict[str, list[str]] = {}
    for cat, label in seen.values():
        by_cat.setdefault(cat, []).append(label)
    ranked = sorted(by_cat.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    top = ranked[:MAX_CATEGORIES * 2]
    return {"reviews": reviews, **block([label for _, label in seen.values()]),
            "by_category": {cat: block(labels) for cat, labels in top}, "categories_truncated": len(ranked) > len(top)}


def _why_empty(reviews: int, labelled: int, replied: int, miss_prs: int, contested: bool, scoped: bool) -> str:
    if not reviews:
        return ("no review-pr runs of this repository are recorded here, and no address-comments run has seen a code-check "
                "review thread; local reviews carry no evidence, which comes from reviews posted to a PR and your verdicts on them")
    parts = []
    if not labelled:
        note = f"; {replied} with a reply but no verdict yet" if replied else ""
        parts.append(f"{reviews} recorded PR review{'s' if reviews != 1 else ''}, but none has a finding{' under this path' if scoped else ''} that was confirmed or disputed (a verdict, a resolved thread or a matching human comment){note}")
    elif contested:
        parts.append("a category was disputed in several PRs but confirmed in at least as many, so it is not a pattern")
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
