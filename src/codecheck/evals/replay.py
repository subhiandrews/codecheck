"""Replay real pull requests through the review-local flow with the real model (developer tooling, costs tokens).

A case is a saved PR: a local clone, the head commit that was reviewed, its base (the merge-base) and the diff the
review saw. The replay rebuilds a throwaway repository whose `origin/main` is the base and whose `HEAD` is the head,
checks that `git diff base...head` is exactly the saved diff, then runs one headless review the way `eval --live`
does, in isolated state. Findings are scored against the human comments recorded for that PR with a plain rule
(same file, line within a tolerance). Nothing is posted and the user's own clones, state and shadow store are never
touched. Use `--dry-run` to rebuild and check every case without calling a model.

    python -m codecheck.evals.replay manifest --shadow DIR --clone OWNER/REPO=PATH RUN_ID... > cases.json
    python -m codecheck.evals.replay run cases.json [--dry-run] [--limit N] [--budget USD] [--out results.jsonl]
"""

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from codecheck import __version__
from codecheck.commands.run_pr import parse_result
from codecheck.errors import CodeCheckError
from codecheck.evals import live
from codecheck.shadow.store import ShadowStore

SHA = re.compile(r"[0-9a-f]{40}")
# Authors whose comments are not human review: matched as lowercase substrings of the login.
BOT_HINTS = ("[bot]", "bot", "copilot", "coderabbit", "codecov", "github-actions", "dependabot", "duo")
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
    if not isinstance(case["comments"], list):
        raise CodeCheckError(f"case {case['name']}: comments must be a list")
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
    return ctx.runner.run([*live._GIT, *args], cwd=cwd).returncode == 0


def _out(ctx, cwd: Path, *args: str) -> str:
    return live.run_checked(ctx.runner, [*live._GIT, *args], cwd=cwd).stdout


def resolve_base(ctx, clone: Path, head: str, diff: str) -> str:
    """The merge-base of `head` and the clone's base branch whose three-dot diff is exactly the saved one."""
    for ref in BASE_REFS:
        res = ctx.runner.run([*live._GIT, "merge-base", head, ref], cwd=clone)
        base = res.stdout.strip()
        if res.returncode != 0 or not SHA.fullmatch(base):
            continue
        shown = ctx.runner.run([*live._GIT, "diff", "--no-color", "--no-ext-diff", f"{base}...{head}"], cwd=clone)
        if shown.returncode == 0 and _same_diff(shown.stdout, diff):
            return base
    raise CodeCheckError(f"no base of {head[:10]} in {clone} reproduces the saved diff; the clone may lack the history")


def build_repo(ctx, case: dict, workdir: Path) -> Path:
    """A clone of the case's repository with `origin/main` at the base and branch `replay` at the head."""
    source = _clone_path(case)
    repo, origin = workdir / "repo", workdir / "origin.git"
    head, base = case["head"], case["base"]
    live._git(ctx, workdir, "clone", "--quiet", "--no-checkout", "--", str(source), str(repo))
    for sha in (head, base):
        if not _ok(ctx, repo, "cat-file", "-e", f"{sha}^{{commit}}"):
            raise CodeCheckError(f"case {case['name']}: commit {sha[:10]} is not in {source}")
    if not _ok(ctx, repo, "merge-base", "--is-ancestor", base, head):
        raise CodeCheckError(f"case {case['name']}: base {base[:10]} is not an ancestor of head {head[:10]}")
    shown = _out(ctx, repo, "diff", "--no-color", "--no-ext-diff", f"{base}...{head}")
    if not _same_diff(shown, case["diff"]):
        raise CodeCheckError(f"case {case['name']}: git diff {base[:10]}...{head[:10]} is not the saved diff")
    live._git(ctx, workdir, "init", "--quiet", "--bare", "-b", "main", str(origin))
    live._git(ctx, repo, "push", "--quiet", str(origin), f"{base}:refs/heads/main")
    live._git(ctx, repo, "remote", "remove", "origin")
    live._git(ctx, repo, "remote", "add", "origin", str(origin))
    live._git(ctx, repo, "fetch", "--quiet", "origin")
    live._git(ctx, repo, "remote", "set-head", "origin", "main")
    live._git(ctx, repo, "checkout", "--quiet", "-b", "replay", head)
    return repo


# --- scoring -----------------------------------------------------------------------------------------------------


def score(findings: list[dict], comments: list[dict], *, tolerance: int = 3, max_per_author: int | None = None) -> dict:
    """Which human comments a finding landed on: same file, lines within `tolerance`.

    Comments outside the reviewed files (`in_review` false) and comments without a line are left out of the
    denominator. `max_per_author` keeps only each author's first comments so one reviewer cannot dominate."""
    kept, per_author = [], {}
    for c in comments:
        if c.get("in_review") is False or not isinstance(c.get("line"), int) or not isinstance(c.get("path"), str):
            continue
        author = c.get("author")
        per_author[author] = per_author.get(author, 0) + 1
        if max_per_author is not None and per_author[author] > max_per_author:
            continue
        kept.append(c)
    rows, used = [], set()
    for c in kept:
        hit = next((i for i, f in enumerate(findings)
                    if f.get("file") == c["path"] and abs(int(f.get("line", -10**9)) - c["line"]) <= tolerance), None)
        if hit is not None:
            used.add(hit)
        rows.append({"thread_id": c.get("thread_id"), "path": c["path"], "line": c["line"], "author": c.get("author"),
                     "matched": hit is not None})
    matched = sum(r["matched"] for r in rows)
    return {"comments": len(rows), "matched": matched, "recall": (matched / len(rows)) if rows else None,
            "findings": len(findings), "findings_on_a_comment": len(used), "findings_elsewhere": len(findings) - len(used),
            "per_comment": rows}


# --- running -----------------------------------------------------------------------------------------------------


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
    got, invalid, error = live._findings(ShadowStore(workdir / "shadow"))
    if not claude["ok"]:
        error = f"claude run failed: {claude['summary']}"
    out |= {"got": got, "claude": {"ok": claude["ok"], "summary": claude["summary"], "cost_usd": claude["cost"]},
            "score": score(got, case["comments"], tolerance=tolerance, max_per_author=max_per_author)}
    if invalid:
        out["invalid"] = invalid[:5]
    if error:
        out["error"] = error
    return out


def run_cases(ctx, cases: list[dict], *, dry_run: bool = False, limit: int | None = None, budget_usd: float | None = None,
              only: str | None = None, fixed_panel: bool = False, tolerance: int = 3, max_per_author: int | None = None) -> dict:
    """One review per case, one after another, never retried. The budget is checked before each case, so the last
    case can overshoot it by one review's cost."""
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
    results, spent, skipped = [], 0.0, []
    for case in cases:
        if budget_usd is not None and spent >= budget_usd:
            skipped.append(case["name"])
            continue
        with tempfile.TemporaryDirectory(prefix="code-check-replay-", ignore_cleanup_errors=True) as workdir:
            try:
                result = run_case(ctx, case, Path(workdir), fixed_panel=fixed_panel, dry_run=dry_run,
                                  tolerance=tolerance, max_per_author=max_per_author)
            except CodeCheckError as e:
                result = {"name": case["name"], "error": str(e)}
        spent += float((result.get("claude") or {}).get("cost_usd") or 0)
        results.append(result)
    return {"cases": results, "cost_usd": round(spent, 4), "skipped_for_budget": skipped, "dry_run": dry_run,
            "tool_version": __version__}


# --- building a manifest from hand-offs --------------------------------------------------------------------------


def human_comments(handoff: dict, *, owner: str | None = None, bots: tuple[str, ...] = BOT_HINTS) -> list[dict]:
    keep = []
    for e in (handoff.get("comparison") or {}).get("human_comments") or []:
        author = (e.get("author") or "").lower()
        if any(b in author for b in bots) or (owner and author == owner.lower()):
            continue
        keep.append({k: e.get(k) for k in ("thread_id", "path", "line", "author", "in_review")})
    return keep


def case_from_handoff(ctx, shadow_dir: Path, run_id: str, clone: Path, *, owner: str | None = None) -> dict:
    if not re.fullmatch(r"[0-9a-f]{12}", run_id):
        raise CodeCheckError(f"run id {run_id!r} is not a 12-character hex id")
    shadow = Path(shadow_dir).expanduser()
    try:
        handoff = json.loads((shadow / f"{run_id}.handoff" / "handoff.json").read_text(encoding="utf-8"))
        diff = (shadow / f"{run_id}.diff").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise CodeCheckError(f"run {run_id}: no readable hand-off or diff in {shadow}: {e}") from e
    head = handoff.get("head_sha")
    if not isinstance(head, str) or not SHA.fullmatch(head):
        raise CodeCheckError(f"run {run_id}: hand-off has no head commit")
    clone = Path(clone).expanduser()
    base = resolve_base(ctx, clone, head, diff)
    pr = str(handoff.get("pr"))
    name = f"{pr if '#' in pr else handoff.get('repo') + '#' + pr}@{run_id}"  # `pr` is OWNER/REPO#N in newer hand-offs
    return {"name": name, "repo": handoff.get("repo"), "clone": str(clone), "head": head, "base": base,
            "diff": diff, "comments": human_comments(handoff, owner=owner), "recorded_tool_version": handoff.get("tool_version")}


# --- command line ------------------------------------------------------------------------------------------------


def _context():
    from codecheck.cli import build_context

    return build_context(Path.cwd().resolve(), os.environ, Path.home())


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
    r.add_argument("--limit", type=int, default=None)
    r.add_argument("--budget", type=float, default=None, metavar="USD", help="stop starting new cases past this total")
    r.add_argument("--only", default=None, metavar="NAME")
    r.add_argument("--fixed-panel", action="store_true", help="the live-eval panel (routing off) instead of the shipped one")
    r.add_argument("--tolerance", type=int, default=3)
    r.add_argument("--max-per-author", type=int, default=None)
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
                clones[repo] = Path(path)
            cases = []
            for run_id in args.runs:
                handoff = json.loads((args.shadow.expanduser() / f"{run_id}.handoff" / "handoff.json").read_text(encoding="utf-8"))
                clone = clones.get(handoff.get("repo"))
                if clone is None:
                    raise CodeCheckError(f"run {run_id}: no --clone for {handoff.get('repo')}")
                cases.append(case_from_handoff(ctx, args.shadow, run_id, clone, owner=args.owner))
            json.dump(cases, sys.stdout, indent=2)
            print()
            return 0
        result = run_cases(ctx, load_cases(args.cases), dry_run=args.dry_run, limit=args.limit, budget_usd=args.budget,
                           only=args.only, fixed_panel=args.fixed_panel, tolerance=args.tolerance,
                           max_per_author=args.max_per_author)
        if args.out is not None:
            with args.out.expanduser().open("a", encoding="utf-8") as fh:
                for c in result["cases"]:
                    fh.write(json.dumps(c) + "\n")
        print(json.dumps(result, indent=2))
        return 0
    except (CodeCheckError, OSError, json.JSONDecodeError) as e:
        print(json.dumps({"error": str(e)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
