"""Replay real pull requests through the review-local flow with the real model (developer tooling, costs tokens).

A case is a saved PR: a local clone, the head commit that was reviewed, its base (the merge-base) and the diff the
review saw. The replay rebuilds a throwaway repository whose `origin/main` is the base and whose `HEAD` is the head,
checks that `git diff base...head` is exactly the saved diff, then runs one headless review the way `eval --live`
does, in isolated state. Findings are scored against the human comments recorded for that PR with a plain rule
(same file, line within a tolerance). Nothing is posted and the user's own clones, state and shadow store are never
touched. Use `--dry-run` to rebuild and check every case without calling a model.

    python -m codecheck.evals.replay manifest|run --help

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


def score(findings: list[dict], comments: list[dict], *, tolerance: int = 3, max_per_author: int | None = None) -> dict:
    """Which human comments a finding landed on: same file, lines within `tolerance`.

    Comments outside the reviewed files (`in_review` false) and comments without a line are left out of the
    denominator. `max_per_author` keeps only each author's first comments so one reviewer cannot dominate. Every
    finding near any kept comment counts as on a comment; a finding without an integer line lands on none."""
    kept, per_author = [], {}
    for c in comments:
        if c.get("in_review") is False or not isinstance(c.get("line"), int) or not isinstance(c.get("path"), str):
            continue
        author = c.get("author") if isinstance(c.get("author"), str) else ""
        per_author[author] = per_author.get(author, 0) + 1
        if max_per_author is not None and per_author[author] > max_per_author:
            continue
        kept.append(c)
    rows, used = [], set()
    for c in kept:
        hits = [i for i, f in enumerate(findings)
                if f.get("file") == c["path"] and isinstance(f.get("line"), int) and abs(f["line"] - c["line"]) <= tolerance]
        used.update(hits)
        rows.append({"thread_id": c.get("thread_id"), "path": c["path"], "line": c["line"], "author": c.get("author"),
                     "matched": bool(hits)})
    matched = sum(r["matched"] for r in rows)
    return {"comments": len(rows), "matched": matched, "recall": (matched / len(rows)) if rows else None,
            "findings": len(findings), "findings_on_a_comment": len(used), "findings_elsewhere": len(findings) - len(used),
            "per_comment": rows}


# --- running -----------------------------------------------------------------------------------------------------


def _cost(value) -> float | None:
    """A review's cost when the model reported a real number, else None (never a guess that it was free)."""
    ok = isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
    return float(value) if ok else None


def run_case(ctx, case: dict, workdir: Path, *, fixed_panel: bool = False, dry_run: bool = False,
             tolerance: int = 3, max_per_author: int | None = None) -> dict:
    workdir = Path(workdir).resolve()
    repo = build_repo(ctx, case, workdir)
    out = {"name": case["name"], "repo": case["repo"], "head": case["head"], "base": case["base"],
           "tool_version": __version__, "diff_bytes": len(case["diff"])}
    if dry_run:
        return {**out, "built": True}
    res = ctx.runner.run(live.review_argv(ctx, workdir, fixed_panel=fixed_panel), cwd=repo,
                         timeout=ctx.config.run_pr.timeout_seconds, input="")
    claude = parse_result(res.stdout, res.returncode, res.stderr)
    got, invalid, error = live.review_findings(ShadowStore(workdir / "shadow"))
    if not claude["ok"]:
        error = f"claude run failed: {claude['summary']}"
    out |= {"got": got, "claude": {"ok": claude["ok"], "summary": claude["summary"], "cost_usd": _cost(claude["cost"])},
            "score": score(got, case["comments"], tolerance=tolerance, max_per_author=max_per_author)}
    if invalid:
        out["invalid"] = invalid[:5]
    if error:
        out["error"] = error
    return out


def run_cases(ctx, cases: list[dict], *, dry_run: bool = False, limit: int | None = None, budget_usd: float | None = None,
              only: str | None = None, fixed_panel: bool = False, tolerance: int = 3, max_per_author: int | None = None,
              on_result=None) -> dict:
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
                                  tolerance=tolerance, max_per_author=max_per_author)
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
            on_result(result)
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
    r = sub.add_parser("run", help="rebuild each case and review it (costs tokens unless --dry-run)")
    r.add_argument("cases", type=Path)
    r.add_argument("--dry-run", action="store_true", help="rebuild and check every case; no model")
    r.add_argument("--limit", type=_non_negative(int), default=None)
    r.add_argument("--budget", type=_non_negative(float), default=None, metavar="USD", help="stop starting new cases past this total")
    r.add_argument("--only", default=None, metavar="NAME")
    r.add_argument("--fixed-panel", action="store_true", help="the live-eval panel (routing off) instead of the shipped one")
    r.add_argument("--tolerance", type=_non_negative(int), default=3)
    r.add_argument("--max-per-author", type=_non_negative(int), default=None)
    r.add_argument("--out", type=Path, default=None, help="append one JSON line per case here")
    args = parser.parse_args(argv)
    try:
        ctx = _context()
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
                           max_per_author=args.max_per_author, on_result=save)
        print(json.dumps(result, indent=2))
        return 0
    except (CodeCheckError, OSError, json.JSONDecodeError) as e:
        print(json.dumps({"error": str(e)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
