"""Evaluate the review pipeline. `--replay` replays the golden cases offline; `--live` reviews the snippets with the
real model (local only, costs tokens). The result's `passed` is the verdict."""

import tempfile
from pathlib import Path

from codecheck.errors import CodeCheckError
from codecheck.evals.golden import DEFAULT_CASES, run_replay
from codecheck.evals.live import (
    DEFAULT_SNIPPETS,
    load_snippets,
    run_snippet,
    unreachable_reason,
)

NAME = "eval"
HELP = "Evaluate the review pipeline against the golden cases or live snippets (developer tooling)"


def add_arguments(p) -> None:
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--replay", action="store_true",
                      help="replay stored reviewer outputs through the deterministic pipeline (no network, no model)")
    mode.add_argument("--live", action="store_true",
                      help="review each snippet with the real panel through `claude -p` (local only; costs tokens)")
    p.add_argument("--cases", type=Path, default=None,
                   help="--replay: directory of golden case files (default: the plugin repository's tests/golden)")
    p.add_argument("--snippets", type=Path, default=None,
                   help="--live: directory of snippet folders (default: the plugin repository's tests/snippets)")
    p.add_argument("--only", default=None, metavar="NAME", help="--live: run just this snippet")
    p.add_argument("--if-reachable", action="store_true",
                   help="--live: when no model is reachable, report a skip instead of failing")


def _dir(ctx, given: Path | None, default: Path, what: str) -> Path:
    if given is None:
        return default  # missing in an installed package: then there is nothing to run, which passes
    path = given if given.is_absolute() else ctx.cwd / given
    if not path.is_dir():
        raise CodeCheckError(f"no such {what} directory: {given}")
    return path


def run(args, ctx) -> dict:
    if args.replay:
        if args.snippets is not None or args.only is not None or args.if_reachable:
            raise CodeCheckError("--snippets, --only and --if-reachable apply to --live only")
        # Exit status stays 0 when a case fails: callers (CI, the commit gate) read `passed`.
        return run_replay(_dir(ctx, args.cases, DEFAULT_CASES, "cases"))
    if args.cases is not None:
        raise CodeCheckError("--cases applies to --replay only")
    return run_live(ctx, _dir(ctx, args.snippets, DEFAULT_SNIPPETS, "snippets"), args.only, args.if_reachable)


def run_live(ctx, directory: Path, only: str | None, if_reachable: bool) -> dict:
    snippets = load_snippets(directory)
    if only is not None:
        names = [s["name"] for s in snippets]
        if only not in names:
            raise CodeCheckError(f"unknown snippet {only!r}; known: {', '.join(names) or 'none'}")
        snippets = [s for s in snippets if s["name"] == only]
    if not snippets:
        return {"passed": True, "cases": []}
    reason = unreachable_reason(ctx)
    if reason is not None:
        if if_reachable:
            return {"passed": True, "skipped": f"model not reachable: {reason}", "cases": []}
        raise CodeCheckError(f"model not reachable: {reason}")
    # One review per snippet, one after another, never retried: each costs real tokens.
    cases = []
    for snippet in snippets:
        with tempfile.TemporaryDirectory(prefix="code-check-eval-", ignore_cleanup_errors=True) as workdir:
            cases.append(run_snippet(ctx, snippet, Path(workdir)))
    # Exit status stays 0 when a case fails, as with --replay: read `passed`.
    return {"passed": all(c["result"]["passed"] for c in cases), "cases": cases}
