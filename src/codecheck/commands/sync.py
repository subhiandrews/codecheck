from codecheck.vcs.sync import sync_base

NAME = "sync"
HELP = "Fetch the latest remote base branch"


def add_arguments(p) -> None:
    p.add_argument("--base", default=None, help="base branch (default: config base_branch)")


def run(args, ctx) -> dict:
    res = sync_base(ctx.git, ctx.config.remote, args.base or ctx.config.base_branch)
    return {"base_ref": res.base_ref, "base_sha": res.base_sha, "fresh": res.fresh, "warning": res.warning}
