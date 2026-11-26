import argparse
import shutil
from pathlib import Path

from codecheck.commands import handoff
from codecheck.commands._common import load_meta

NAME = "cleanup"
HELP = "Remove a run's temporary worktree and its run directory"


def add_arguments(p) -> None:
    p.add_argument("--run", required=True)


def _ensure_handoff(args, ctx, meta: dict) -> dict:
    """Write the run's hand-off if the skill's step skipped it: the run directory is its only source.

    Best effort: `handoff` never raises, so a failure here cannot stop the cleanup."""
    if not meta.get("handoff") or not ctx.config.shadow.handoff or ctx.shadow is None:
        return {}
    if ctx.shadow.has_handoff(args.run):
        return {}
    res = handoff.run(argparse.Namespace(run=args.run), ctx)
    return {"handoff": res.get("handoff", False), **({"handoff_error": res["error"]} if "error" in res else {})}


def run(args, ctx) -> dict:
    meta = load_meta(ctx, args.run)
    extra = _ensure_handoff(args, ctx, meta)
    removed = False
    if meta.get("worktree"):
        ctx.git.worktree_remove(Path(meta["root"]))
        removed = not Path(meta["root"]).exists()
        if not removed:
            # Keep the run directory: its context.json is how a later cleanup finds the worktree.
            return {"removed": False, "run_dir_removed": False, **extra}
    run_dir = ctx.run_dir(args.run)
    shutil.rmtree(run_dir, ignore_errors=True)
    return {"removed": removed, "run_dir_removed": not run_dir.exists(), **extra}
