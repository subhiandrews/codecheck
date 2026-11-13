import shutil
from pathlib import Path

from codecheck.commands._common import load_meta

NAME = "cleanup"
HELP = "Remove a run's temporary worktree and its run directory"


def add_arguments(p) -> None:
    p.add_argument("--run", required=True)


def run(args, ctx) -> dict:
    meta = load_meta(ctx, args.run)
    removed = False
    if meta.get("worktree"):
        ctx.git.worktree_remove(Path(meta["root"]))
        removed = not Path(meta["root"]).exists()
        if not removed:
            # Keep the run directory: its context.json is how a later cleanup finds the worktree.
            return {"removed": False, "run_dir_removed": False}
    run_dir = ctx.run_dir(args.run)
    shutil.rmtree(run_dir, ignore_errors=True)
    return {"removed": removed, "run_dir_removed": not run_dir.exists()}
