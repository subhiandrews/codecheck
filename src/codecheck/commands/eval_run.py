"""Evaluate the review pipeline. `--replay` replays the golden cases offline; the result's `passed` is the verdict."""

from pathlib import Path

from codecheck.errors import CodeCheckError
from codecheck.evals.golden import DEFAULT_CASES, run_replay

NAME = "eval"
HELP = "Evaluate the review pipeline against the golden cases (developer tooling)"


def add_arguments(p) -> None:
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--replay", action="store_true",
                      help="replay stored reviewer outputs through the deterministic pipeline (no network, no model)")
    p.add_argument("--cases", type=Path, default=None,
                   help="directory of golden case files (default: the plugin repository's tests/golden)")


def run(args, ctx) -> dict:
    if args.cases is None:
        cases = DEFAULT_CASES  # missing in an installed package: then there are no cases, which passes
    else:
        cases = args.cases if args.cases.is_absolute() else ctx.cwd / args.cases
        if not cases.is_dir():
            raise CodeCheckError(f"no such cases directory: {args.cases}")
    # Exit status stays 0 when a case fails: callers (CI, the commit gate) read `passed`.
    return run_replay(cases)
