"""Queue and merge guard for unattended hand-off rounds (developer tooling, hidden from `--help`)."""

import argparse
import time

from codecheck.errors import CodeCheckError
from codecheck.evals.golden import run_replay
from codecheck.shadow.queue import (
    claim_next,
    finish,
    release,
    require_claim,
    summarize_diff,
    violations,
)

NAME = "handoff-queue"
HELP = argparse.SUPPRESS  # an internal skill step; hidden from `code-check --help`


def add_arguments(p) -> None:
    sub = p.add_subparsers(dest="queue_cmd", required=True)
    sub.add_parser("next")
    d = sub.add_parser("done")
    d.add_argument("--run", required=True)
    d.add_argument("--claim", required=True)
    d.add_argument("--pr", required=True)
    r = sub.add_parser("release")
    r.add_argument("--run", required=True)
    r.add_argument("--claim", required=True)
    r.add_argument("--reason", default="")
    c = sub.add_parser("check")
    c.add_argument("--run", required=True)
    c.add_argument("--claim", required=True)
    c.add_argument("--pr", required=True)


def _shadow(ctx):
    if ctx.shadow is None:
        raise CodeCheckError("shadow store is off: set shadow.enabled or shadow.handoff")
    return ctx.shadow


def run(args, ctx) -> dict:
    if args.queue_cmd == "next":
        if ctx.shadow is None:
            return {"run": None, "skipped": {}, "reason": "shadow store is off"}
        return claim_next(ctx.shadow, time.time())
    if args.queue_cmd == "done":
        return _done(args, ctx)
    if args.queue_cmd == "release":
        release(_shadow(ctx), args.run, args.reason, time.time(), args.claim)
        return {"released": True, "run": args.run}
    return _check(args, ctx)


def _done(args, ctx) -> dict:
    """Mark analyzed, but only for a PR of this repository that really was merged."""
    shadow = _shadow(ctx)
    require_claim(shadow, args.run, args.claim)
    ref = ctx.resolve_ref(args.pr)
    if ref.repo != ctx.current_repo():
        raise CodeCheckError(f"{ref} is not a pull request of this repository")
    state = ctx.provider.get_pr(ref).state
    if state != "merged":
        raise CodeCheckError(f"{ref} is not merged (state: {state})")
    finish(shadow, args.run, args.pr, time.time(), args.claim)
    return {"done": True, "run": args.run}


def _check(args, ctx) -> dict:
    """Whether the PR may be merged unattended.

    The guard decides what is judged: it reads the PR from the provider, fetches the base itself, and diffs the
    base against the PR's head commit, which must be what this checkout has. Nothing about the range comes from the
    caller. Run it from the installed plugin, not from the branch it judges.
    """
    require_claim(_shadow(ctx), args.run, args.claim)
    ref = ctx.resolve_ref(args.pr)
    if ref.repo != ctx.current_repo():
        raise CodeCheckError(f"{ref} is not a pull request of this repository")
    pr = ctx.provider.get_pr(ref)
    remote = ctx.config.remote
    base = ctx.config.base_branch if ctx.config.base_branch != "auto" else ctx.git.default_branch(remote)
    problems: list[str] = []
    if pr.state != "open":
        problems.append(f"the PR is not open (state: {pr.state})")
    if pr.base_ref != base:
        problems.append(f"the PR targets base {pr.base_ref!r}, not {base!r}")
    if ctx.git.head_sha() != pr.head_sha:
        problems.append("local HEAD is not the PR head: check out the PR head and run the guard again")
    changes = []
    if not ctx.git.fetch(remote, f"+refs/heads/{base}:refs/remotes/{remote}/{base}"):
        problems.append(f"could not fetch the base branch {base!r}")
    else:
        try:
            changes = summarize_diff(ctx.git.diff(f"{remote}/{base}...{pr.head_sha}"))
        except CodeCheckError as e:
            problems.append(f"cannot diff the PR head against the base: {e}")
    if not changes and not problems:
        problems.append("the PR has no changes against the base")
    problems += violations(changes)
    cases = ctx.cwd / "tests" / "golden"
    replay_failed: list = []
    try:
        replay = run_replay(cases) if cases.is_dir() else {"passed": True, "failed": []}
        replay_passed, replay_failed = bool(replay["passed"]), replay.get("failed", [])
    except CodeCheckError as e:
        replay_passed, replay_failed = False, [{"case": "(load)", "problems": [str(e)]}]
    return {"ok": not problems and replay_passed, "violations": problems, "head_sha": pr.head_sha, "base": base,
            "files": len(changes), "lines": sum(c.added + c.removed for c in changes),
            "replay_passed": replay_passed, "replay_failed": replay_failed}
