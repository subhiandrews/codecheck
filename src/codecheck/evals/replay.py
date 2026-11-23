"""Replay real pull requests through the review-local flow with the real model (developer tooling, costs tokens).

A case is a saved PR: a local clone, the head commit that was reviewed, its base (the merge-base) and the diff the
review saw. The replay rebuilds a throwaway repository whose `origin/main` is the base and whose `HEAD` is the head,
checks that `git diff base...head` is exactly the saved diff, then runs one headless review the way `eval --live`
does, in isolated state. Findings are scored against the human comments recorded for that PR with a plain rule
(same file, line within a tolerance, at the line a finding is anchored to or at a `file:line` its message names;
`anchor_recall` counts the anchor only). Nothing is posted and the user's own clones, state and shadow store are never
touched. Use `--dry-run` to rebuild and check every case without calling a model.

    python -m codecheck.evals.replay manifest|from-git|run --help

The commands and options are described in the "PR replay" section of docs/MAINTAINING.md.
"""

import argparse
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path

from codecheck import __version__
from codecheck.commands.run_pr import parse_result
from codecheck.errors import CodeCheckError
from codecheck.evals import live
from codecheck.models import is_service_login
from codecheck.runner import run_checked
from codecheck.shadow.store import ShadowStore

SHA = re.compile(r"[0-9a-f]{40}")
# Review bots whose login has no `[bot]` suffix, compared whole and in lowercase; `is_service_login` covers the rest.
KNOWN_BOTS = frozenset({"copilot", "copilot-pull-request-reviewer", "coderabbitai", "codecov-commenter", "codecov",
                        "github-actions", "dependabot"})
BASE_REFS = ("origin/main", "origin/master", "main", "master")
REQUIRED = ("name", "repo", "clone", "head", "base", "diff", "comments")


def _same_diff(a: str, b: str) -> bool:
    """Equal up to trailing whitespace per line and at the ends."""
    return [x.rstrip() for x in a.strip().splitlines()] == [x.rstrip() for x in b.strip().splitlines()]


def _clone_path(case: dict) -> Path:
    raw = str(case["clone"])
    path = Path(raw).expanduser()
    if raw.startswith("-") or not path.is_absolute() or not path.is_dir():
        raise CodeCheckError(f"case {case['name']}: clone {raw!r} must be an absolute path to an existing directory")
    return path


def validate_case(case: dict) -> dict:
    if not isinstance(case, dict):
        raise CodeCheckError("a case must be an object")
    missing = [k for k in REQUIRED if k not in case]
    if missing:
        raise CodeCheckError(f"case {case.get('name', '?')}: missing {', '.join(missing)}")
    for key in ("head", "base"):
        if not isinstance(case[key], str) or not SHA.fullmatch(case[key]):
            raise CodeCheckError(f"case {case['name']}: {key} must be a full lowercase 40-character commit id")
    if not isinstance(case["name"], str):
        raise CodeCheckError("a case name must be a string")
    if not isinstance(case["comments"], list) or not all(isinstance(c, dict) for c in case["comments"]):
        raise CodeCheckError(f"case {case['name']}: comments must be a list of objects")
    if not isinstance(case["diff"], str) or not isinstance(case["repo"], str):
        raise CodeCheckError(f"case {case['name']}: repo and diff must be strings")
    _clone_path(case)
    return case


def load_cases(path: Path) -> list[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise CodeCheckError(f"cases file {path}: not readable JSON: {e}") from e
    if not isinstance(data, list):
        raise CodeCheckError(f"cases file {path}: expected a list")
    cases = [validate_case(c) for c in data]
    names = [c["name"] for c in cases]
    if len(set(names)) != len(names):
        raise CodeCheckError(f"cases file {path}: duplicate case names")
    return cases


# --- rebuilding the repository -----------------------------------------------------------------------------------


def _ok(ctx, cwd: Path, *args: str) -> bool:
    return ctx.runner.run([*live.GIT, *args], cwd=cwd).returncode == 0


def _out(ctx, cwd: Path, *args: str) -> str:
    return run_checked(ctx.runner, [*live.GIT, *args], cwd=cwd).stdout


def resolve_base(ctx, clone: Path, head: str, diff: str) -> str:
    """The merge-base of `head` and the clone's base branch whose three-dot diff is exactly the saved one."""
    for ref in BASE_REFS:
        res = ctx.runner.run([*live.GIT, "merge-base", head, ref], cwd=clone)
        base = res.stdout.strip()
        if res.returncode != 0 or not SHA.fullmatch(base):
            continue
        shown = ctx.runner.run([*live.GIT, "diff", "--no-color", "--no-ext-diff", f"{base}...{head}"], cwd=clone)
        if shown.returncode == 0 and _same_diff(shown.stdout, diff):
            return base
    raise CodeCheckError(f"no base of {head[:10]} in {clone} reproduces the saved diff; the clone may lack the history")


def build_repo(ctx, case: dict, workdir: Path) -> Path:
    """A clone of the case's repository with `origin/main` at the base and branch `replay` at the head."""
    source = _clone_path(case)
    repo, origin = workdir / "repo", workdir / "origin.git"
    head, base = case["head"], case["base"]
    live.git(ctx, workdir, "clone", "--quiet", "--no-checkout", "--", str(source), str(repo))
    for sha in (head, base):
        if not _ok(ctx, repo, "cat-file", "-e", f"{sha}^{{commit}}"):
            raise CodeCheckError(f"case {case['name']}: commit {sha[:10]} is not in {source}")
    if not _ok(ctx, repo, "merge-base", "--is-ancestor", base, head):
        raise CodeCheckError(f"case {case['name']}: base {base[:10]} is not an ancestor of head {head[:10]}")
    shown = _out(ctx, repo, "diff", "--no-color", "--no-ext-diff", f"{base}...{head}")
    if not _same_diff(shown, case["diff"]):
        raise CodeCheckError(f"case {case['name']}: git diff {base[:10]}...{head[:10]} is not the saved diff")
    live.git(ctx, workdir, "init", "--quiet", "--bare", "-b", "main", str(origin))
    live.git(ctx, repo, "push", "--quiet", str(origin), f"{base}:refs/heads/main")
    live.git(ctx, repo, "remote", "remove", "origin")
    live.git(ctx, repo, "remote", "add", "origin", str(origin))
    live.git(ctx, repo, "fetch", "--quiet", "origin")
    live.git(ctx, repo, "remote", "set-head", "origin", "main")
    live.git(ctx, repo, "checkout", "--quiet", "-b", "replay", head)
    return repo


# --- scoring -----------------------------------------------------------------------------------------------------


SITE = re.compile(r"([\w./-]+):(\d{1,9})(?!\d)")  # also files without an extension, such as docker/Dockerfile


def _sites(finding: dict) -> list[tuple[str, int]]:
    """Where a finding points: its anchor plus every `file:line` its message names (a pattern reported once)."""
    sites = []
    if isinstance(finding.get("file"), str) and isinstance(finding.get("line"), int):
        sites.append((finding["file"], finding["line"]))
    sites += [(m[0], int(m[1])) for m in SITE.findall(str(finding.get("message") or ""))]
    return sites


def score(findings: list[dict], comments: list[dict], *, tolerance: int = 3, max_per_author: int | None = None) -> dict:
    """Which human comments a finding landed on: same file, lines within `tolerance`, at the finding's anchor or at
    a `file:line` its message names.

    Comments outside the reviewed files (`in_review` false) and comments without a line are left out of the
    denominator. `max_per_author` keeps only each author's first comments so one reviewer cannot dominate. Every
    finding near any kept comment counts as on a comment; a finding without an integer line lands on none. A
    comment with a `round` (the review round that first reported it) also counts in `by_round`, and one with a
    `kind` (`first-head`, `fix` or `tail`) in `by_kind`."""
    kept, per_author = [], {}
    for c in comments:
        if c.get("in_review") is False or not isinstance(c.get("line"), int) or not isinstance(c.get("path"), str):
            continue
        author = c.get("author") if isinstance(c.get("author"), str) else ""
        per_author[author] = per_author.get(author, 0) + 1
        if max_per_author is not None and per_author[author] > max_per_author:
            continue
        kept.append(c)
    sites = [_sites(f) for f in findings]
    rows, used = [], set()
    for c in kept:
        hits = [i for i, ss in enumerate(sites)
                if any(p == c["path"] and abs(n - c["line"]) <= tolerance for p, n in ss)]
        used.update(hits)
        at_anchor = any(isinstance(f.get("line"), int) and f.get("file") == c["path"]
                        and abs(f["line"] - c["line"]) <= tolerance for f in findings)
        row = {"thread_id": c.get("thread_id"), "path": c["path"], "line": c["line"], "author": c.get("author"),
               "matched": bool(hits), "anchor_matched": at_anchor}
        if c.get("round") is not None:
            row["round"] = c["round"]
        if c.get("kind") is not None:
            row["kind"] = c["kind"]
        rows.append(row)
    matched = sum(r["matched"] for r in rows)
    anchored = sum(r["anchor_matched"] for r in rows)
    by_round: dict = {}
    by_kind: dict = {}
    for r in rows:
        for key, groups in (("round", by_round), ("kind", by_kind)):
            if key in r:
                entry = groups.setdefault(r[key], {"comments": 0, "matched": 0})
                entry["comments"] += 1
                entry["matched"] += r["matched"]
    return {"comments": len(rows), "matched": matched, "recall": (matched / len(rows)) if rows else None,
            "anchor_matched": anchored, "anchor_recall": (anchored / len(rows)) if rows else None,
            "findings": len(findings), "findings_on_a_comment": len(used), "findings_elsewhere": len(findings) - len(used),
            "by_round": {k: by_round[k] for k in sorted(by_round, key=str)},
            "by_kind": {k: by_kind[k] for k in sorted(by_kind, key=str)}, "per_comment": rows}


def _near(findings: list[dict], comment: dict, tolerance: int) -> list[dict]:
    return [f for f in findings if any(p == comment["path"] and abs(n - comment["line"]) <= tolerance
                                       for p, n in _sites(f))]


def funnel(raw: dict | None, comments: list[dict], *, tolerance: int = 3) -> dict:
    """Where each labelled finding was lost: never reported by a reviewer, reported and dropped by the aggregator
    (with its reason), or kept. Also the panel's size, so a thin review can be told from a thin panel."""
    if raw is None:
        return {"error": "no raw output"}
    reviewers = {name: [f for f in found if isinstance(f, dict)] for name, found in (raw.get("reviewers") or {}).items()
                 if isinstance(found, list)}
    aggregator = raw.get("aggregator") if isinstance(raw.get("aggregator"), dict) else {}
    kept = [f for f in aggregator.get("findings") or [] if isinstance(f, dict)]
    dropped = [f for f in aggregator.get("dropped") or [] if isinstance(f, dict)]
    rows = []
    for c in comments:
        if c.get("in_review") is False or not isinstance(c.get("line"), int) or not isinstance(c.get("path"), str):
            continue
        by = sorted(n for n, found in reviewers.items() if _near(found, c, tolerance))
        if _near(kept, c, tolerance):
            stage = "kept"
        elif d := _near(dropped, c, tolerance):
            stage = f"dropped:{d[0].get('reason')}"
        elif by:
            stage = "lost-in-aggregation"
        else:
            stage = "not-reported"
        rows.append({"thread_id": c.get("thread_id"), "round": c.get("round"), "stage": stage, "reported_by": by})
    stages: dict = {}
    for r in rows:
        stages[r["stage"]] = stages.get(r["stage"], 0) + 1
    return {"reviewer_findings": {n: len(f) for n, f in sorted(reviewers.items())},
            "kept": len(kept), "dropped": len(dropped), "stages": dict(sorted(stages.items())),
            "dropped_findings": [{k: d.get(k) for k in ("reviewers", "file", "line", "reason")} for d in dropped],
            "per_comment": rows}


# --- running -----------------------------------------------------------------------------------------------------


def _cost(value) -> float | None:
    """A review's cost when the model reported a real number, else None (never a guess that it was free)."""
    ok = isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
    return float(value) if ok else None


def run_case(ctx, case: dict, workdir: Path, *, fixed_panel: bool = False, dry_run: bool = False,
             tolerance: int = 3, max_per_author: int | None = None, raw_dir: Path | None = None) -> dict:
    workdir = Path(workdir).resolve()
    repo = build_repo(ctx, case, workdir)
    out = {"name": case["name"], "repo": case["repo"], "head": case["head"], "base": case["base"],
           "tool_version": __version__, "diff_bytes": len(case["diff"])}
    if dry_run:
        return {**out, "built": True}
    res = ctx.runner.run(live.review_argv(ctx, workdir, fixed_panel=fixed_panel), cwd=repo,
                         timeout=ctx.config.run_pr.timeout_seconds, input="")
    claude = parse_result(res.stdout, res.returncode, res.stderr)
    # The review has been paid for: record it before anything below can fail, so its cost is always counted.
    out["claude"] = {"ok": claude["ok"], "summary": claude["summary"], "cost_usd": _cost(claude["cost"])}
    try:
        store = ShadowStore(workdir / "shadow")
        got, invalid, error = live.review_findings(store, keep_message=True)
        raw = live.newest_raw(store)
        saved_raw = None
        if raw is not None and raw_dir is not None:
            try:  # a raw file that cannot be written must not throw away a review that was paid for
                target = Path(raw_dir).expanduser()
                target.mkdir(parents=True, exist_ok=True)
                saved = target / (re.sub(r"[^\w.@-]", "_", case["name"]) + ".raw.json")
                saved.write_text(json.dumps(raw, indent=2), encoding="utf-8")
            except OSError as e:
                saved_raw = f"raw output not saved: {e}"
        if not claude["ok"]:
            error = f"claude run failed: {claude['summary']}"
        out |= {"got": got, "score": score(got, case["comments"], tolerance=tolerance, max_per_author=max_per_author),
                "funnel": funnel(raw, case["comments"], tolerance=tolerance)}
        if saved_raw:
            out["raw_error"] = saved_raw
        if invalid:
            out["invalid"] = invalid[:5]
    except Exception as e:  # noqa: BLE001 - whatever breaks reading or scoring must not lose the paid review's cost
        error = f"{type(e).__name__}: {e}"
    if error:
        out["error"] = error
    return out


def run_cases(ctx, cases: list[dict], *, dry_run: bool = False, limit: int | None = None, budget_usd: float | None = None,
              only: str | None = None, fixed_panel: bool = False, tolerance: int = 3, max_per_author: int | None = None,
              raw_dir: Path | None = None, on_result=None) -> dict:
    """One review per case, one after another, never retried. The budget is checked before each case, so the last
    case can overshoot it by one review's cost; `unpriced` lists reviews that reported no cost. `on_result` is called with each result as soon as it exists, so a
    caller can save a paid review before a later case fails."""
    if only is not None:
        cases = [c for c in cases if c["name"] == only]
        if not cases:
            raise CodeCheckError(f"unknown case {only!r}")
    if limit is not None:
        cases = cases[:limit]
    if not dry_run:
        reason = live.unreachable_reason(ctx)
        if reason is not None:
            raise CodeCheckError(f"model not reachable: {reason}")
    results, spent, skipped, unpriced = [], 0.0, [], []
    for case in cases:
        # A review with no reported cost (a timeout, non-JSON output) may still have spent tokens, so with a budget set
        # nothing new starts once one has run: the total is no longer known.
        if budget_usd is not None and (spent >= budget_usd or unpriced):
            skipped.append(case["name"])
            continue
        with tempfile.TemporaryDirectory(prefix="code-check-replay-", ignore_cleanup_errors=True) as workdir:
            try:
                result = run_case(ctx, case, Path(workdir), fixed_panel=fixed_panel, dry_run=dry_run,
                                  tolerance=tolerance, max_per_author=max_per_author, raw_dir=raw_dir)
            except CodeCheckError as e:
                result = {"name": case["name"], "error": str(e)}
        if "claude" in result:  # a review ran (a case that failed to build has no `claude` entry)
            cost = result["claude"].get("cost_usd")
            if cost is None:
                unpriced.append(case["name"])
            else:
                spent += cost
        results.append(result)
        if on_result is not None:
            try:
                on_result(result)
            except OSError as e:  # the result is still in the summary below
                print(f"replay: could not save the result of {case['name']}: {e}", file=sys.stderr)
    return {"cases": results, "cost_usd": round(spent, 4), "skipped_for_budget": skipped,
            "unpriced": unpriced, "dry_run": dry_run,
            "tool_version": __version__}


# --- building a manifest from hand-offs --------------------------------------------------------------------------


def is_bot(author: str, bots: frozenset[str] = KNOWN_BOTS) -> bool:
    """A `name[bot]` login, GitLab Duo, or a login in `bots`, compared whole: never a substring of a person's name."""
    return is_service_login(author) or author.lower() in bots


def human_comments(handoff: dict, *, owner: str | None = None, bots: frozenset[str] = KNOWN_BOTS) -> list[dict]:
    comparison = handoff.get("comparison")
    entries = comparison.get("human_comments") if isinstance(comparison, dict) else None
    keep = []
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict):
            continue
        author = e.get("author") if isinstance(e.get("author"), str) else ""
        if is_bot(author, bots) or (owner and author.lower() == owner.lower()):
            continue
        keep.append({k: e.get(k) for k in ("thread_id", "path", "line", "author", "in_review")})
    return keep


def read_handoff(shadow_dir: Path, run_id: str) -> tuple[dict, str]:
    """The hand-off object and saved diff of a run, or a CodeCheckError for anything unreadable or mistyped."""
    if not re.fullmatch(r"[0-9a-f]{12}", run_id):
        raise CodeCheckError(f"run id {run_id!r} is not a 12-character hex id")
    shadow = Path(shadow_dir).expanduser()
    try:
        handoff = json.loads((shadow / f"{run_id}.handoff" / "handoff.json").read_text(encoding="utf-8"))
        diff = (shadow / f"{run_id}.diff").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise CodeCheckError(f"run {run_id}: no readable hand-off or diff in {shadow}: {e}") from e
    if not isinstance(handoff, dict):
        raise CodeCheckError(f"run {run_id}: the hand-off is not a JSON object")
    return handoff, diff


def case_from_handoff(ctx, shadow_dir: Path, run_id: str, clone: Path | dict[str, Path], *,
                      owner: str | None = None) -> dict:
    """`clone` is one clone, or a mapping of OWNER/REPO to clone for the run's repository."""
    handoff, diff = read_handoff(shadow_dir, run_id)
    head = handoff.get("head_sha")
    if not isinstance(head, str) or not SHA.fullmatch(head):
        raise CodeCheckError(f"run {run_id}: hand-off has no head commit")
    repo = handoff.get("repo")
    if not isinstance(repo, str) or not repo:
        raise CodeCheckError(f"run {run_id}: hand-off has no repository")
    if isinstance(clone, dict):
        if repo not in clone:
            raise CodeCheckError(f"run {run_id}: no --clone for {repo}")
        clone = clone[repo]
    clone = Path(clone).expanduser().resolve()  # a case needs an absolute path, whatever the caller typed
    base = resolve_base(ctx, clone, head, diff)
    pr = str(handoff.get("pr"))
    name = f"{pr if '#' in pr else repo + '#' + pr}@{run_id}"  # `pr` is OWNER/REPO#N in newer hand-offs
    return {"name": name, "repo": repo, "clone": str(clone), "head": head, "base": base,
            "diff": diff, "comments": human_comments(handoff, owner=owner), "recorded_tool_version": handoff.get("tool_version")}


# --- building cases from a clone and a label file ------------------------------------------------------------------


LABEL_KEYS = ("id", "round", "path", "needle")


def validate_labels(labels) -> list[dict]:
    """The label list, or a CodeCheckError naming the first thing wrong with it."""
    if not isinstance(labels, list):
        raise CodeCheckError("labels: expected a list")
    for i, label in enumerate(labels):
        if not isinstance(label, dict):
            raise CodeCheckError(f"label {i}: expected an object")
        missing = [k for k in LABEL_KEYS if k not in label]
        if missing:
            raise CodeCheckError(f"label {label.get('id', i)}: missing {', '.join(missing)}")
        if not all(isinstance(label[k], str) and label[k] for k in ("id", "path", "needle")):
            raise CodeCheckError(f"label {label['id']}: id, path and needle must be non-empty strings")
        if isinstance(label["round"], bool) or not isinstance(label["round"], int):
            raise CodeCheckError(f"label {label['id']}: round must be an integer")
        occurrence = label.get("occurrence", 1)
        if isinstance(occurrence, bool) or not isinstance(occurrence, int) or occurrence < 1:
            raise CodeCheckError(f"label {label['id']}: occurrence must be a positive integer")
    ids = [label["id"] for label in labels]
    if len(set(ids)) != len(ids):
        raise CodeCheckError("labels: duplicate ids")
    return labels


def cases_from_git(ctx, clone: Path, repo: str, base: str, labels: list[dict], heads: list[str], *,
                   name: str = "case") -> list[dict]:
    """One case per head of a PR that was reviewed more than once.

    `labels` are the findings the reviews made over time, found again by text so each head gets its own line
    numbers: {"id", "round" (when it was first reported), "path", "needle" (a line of the file that holds the
    problem), "occurrence" (default 1), "introduced_in" (optional commit: earlier heads do not have the problem yet),
    "fixed_in" (optional commit: heads that contain it no longer have the problem), "tail" (optional: a rare edge
    case the panel is not expected to find)}. A label with `introduced_in` has kind `fix`. Any other head must still hold the needle, or the case is refused. The diff is taken from git, so
    the case always passes the rebuild check."""
    validate_labels(labels)
    for ref in (base, *heads, *(label.get(k) for label in labels for k in ("introduced_in", "fixed_in"))):
        if ref is not None and (not isinstance(ref, str) or not ref or ref.startswith("-")):
            raise CodeCheckError(f"commit {ref!r} must be a non-empty string that does not start with '-'")
    clone = Path(clone).expanduser().resolve()
    if not _ok(ctx, clone, "cat-file", "-e", f"{base}^{{commit}}"):
        raise CodeCheckError(f"base {base!r} is not in {clone}")
    cases = []
    for head in heads:
        full = _out(ctx, clone, "rev-parse", "--verify", f"{head}^{{commit}}").strip()
        diff = _out(ctx, clone, "diff", "--no-color", "--no-ext-diff", f"{base}...{full}")
        comments = []
        for label in labels:
            introduced = label.get("introduced_in")
            if introduced:
                introduced_full = _out(ctx, clone, "rev-parse", "--verify", f"{introduced}^{{commit}}").strip()
                if not _ok(ctx, clone, "merge-base", "--is-ancestor", introduced_full, full):
                    continue
            fixed = label.get("fixed_in")
            if fixed:
                fixed_full = _out(ctx, clone, "rev-parse", "--verify", f"{fixed}^{{commit}}").strip()
                if _ok(ctx, clone, "merge-base", "--is-ancestor", fixed_full, full):
                    continue
            res = ctx.runner.run([*live.GIT, "show", f"{full}:{label['path']}"], cwd=clone)
            lines = res.stdout.splitlines() if res.returncode == 0 else []
            hits = [i for i, text in enumerate(lines, 1) if label["needle"] in text]
            want = int(label.get("occurrence", 1))
            if len(hits) < want:
                raise CodeCheckError(f"label {label['id']}: occurrence {want} of {label['needle']!r} not in {label['path']} at {head}")
            comments.append({"thread_id": label["id"], "path": label["path"], "line": hits[want - 1], "author": "labelled",
                             "in_review": True, "round": label.get("round"),
                             "kind": "fix" if label.get("introduced_in") else ("tail" if label.get("tail") else "first-head")})
        cases.append({"name": f"{name}@{full[:7]}", "repo": repo, "clone": str(clone.resolve()), "head": full, "base": base,
                      "diff": diff, "comments": comments})
    return cases


# --- command line ------------------------------------------------------------------------------------------------


def _context():
    from codecheck.cli import build_context

    return build_context(Path.cwd().resolve(), os.environ, Path.home())


def _non_negative(kind):
    """An argparse type: a finite number >= 0 (rejects nan, inf and negatives, which would disable a cap or slice from the end)."""
    def parse(text: str):
        try:
            value = kind(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r} is not a valid number") from None
        if not math.isfinite(value) or value < 0:
            raise argparse.ArgumentTypeError(f"{text!r} must be a finite number >= 0")
        return value
    return parse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m codecheck.evals.replay", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    m = sub.add_parser("manifest", help="build cases from hand-off runs (no model; prints a JSON list)")
    m.add_argument("--shadow", type=Path, default=Path("~/.local/share/code-check/shadow"))
    m.add_argument("--clone", action="append", default=[], metavar="OWNER/REPO=PATH", help="local clone of a repository")
    m.add_argument("--owner", default=None, help="your own login: its comments are left out")
    m.add_argument("runs", nargs="+", metavar="RUN_ID")
    g = sub.add_parser("from-git", help="build one case per head from a clone and a label file (no model; prints JSON)")
    g.add_argument("--clone", type=Path, required=True)
    g.add_argument("--repo", required=True, metavar="OWNER/REPO")
    g.add_argument("--base", required=True, help="the commit the PR branched from")
    g.add_argument("--labels", type=Path, required=True, help="JSON list of labelled findings")
    g.add_argument("--name", default="case")
    g.add_argument("heads", nargs="+", metavar="HEAD")
    r = sub.add_parser("run", help="rebuild each case and review it (costs tokens unless --dry-run)")
    r.add_argument("cases", type=Path)
    r.add_argument("--dry-run", action="store_true", help="rebuild and check every case; no model")
    r.add_argument("--limit", type=_non_negative(int), default=None)
    r.add_argument("--budget", type=_non_negative(float), default=None, metavar="USD", help="stop starting new cases past this total")
    r.add_argument("--only", default=None, metavar="NAME")
    r.add_argument("--fixed-panel", action="store_true", help="the live-eval panel (routing off) instead of the shipped one")
    r.add_argument("--raw-dir", type=Path, default=None, help="save each case's raw reviewer and aggregator output here")
    r.add_argument("--tolerance", type=_non_negative(int), default=3)
    r.add_argument("--max-per-author", type=_non_negative(int), default=None)
    r.add_argument("--out", type=Path, default=None, help="append one JSON line per case here")
    args = parser.parse_args(argv)
    try:
        ctx = _context()
        if args.command == "from-git":
            try:
                labels = json.loads(args.labels.expanduser().read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
                raise CodeCheckError(f"labels file {args.labels}: not readable JSON: {e}") from e
            cases = cases_from_git(ctx, args.clone, args.repo, args.base, labels, args.heads, name=args.name)
            json.dump(cases, sys.stdout, indent=2)
            print()
            return 0
        if args.command == "manifest":
            clones = {}
            for spec in args.clone:
                repo, sep, path = spec.partition("=")
                if not sep:
                    raise CodeCheckError(f"--clone {spec!r}: expected OWNER/REPO=PATH")
                clones[repo] = Path(path).expanduser().resolve()
            cases = [case_from_handoff(ctx, args.shadow, run_id, clones, owner=args.owner) for run_id in args.runs]
            json.dump(cases, sys.stdout, indent=2)
            print()
            return 0
        out = args.out.expanduser() if args.out is not None else None
        cases = load_cases(args.cases)
        if out is not None:
            out.open("a", encoding="utf-8").close()  # a bad path fails here, before the first paid review

        def save(result: dict) -> None:
            if out is not None:
                with out.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(result) + "\n")

        result = run_cases(ctx, cases, dry_run=args.dry_run, limit=args.limit, budget_usd=args.budget,
                           only=args.only, fixed_panel=args.fixed_panel, tolerance=args.tolerance,
                           max_per_author=args.max_per_author, raw_dir=args.raw_dir, on_result=save)
        print(json.dumps(result, indent=2))
        return 0
    except (CodeCheckError, OSError, json.JSONDecodeError) as e:
        print(json.dumps({"error": str(e)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
