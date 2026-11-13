from pathlib import Path

from codecheck.commands._common import load_meta

NAME = "cleanup"
HELP = "Remove the temporary worktree created for a PR review run"


def add_arguments(p) -> None:
    p.add_argument("--run", required=True)


def run(args, ctx) -> dict:
    meta = load_meta(ctx, args.run)
    if not meta.get("worktree"):
        return {"removed": False}
    ctx.git.worktree_remove(Path(meta["root"]))
    return {"removed": not Path(meta["root"]).exists()}
