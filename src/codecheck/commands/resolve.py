from codecheck.commands.reply import find_thread
from codecheck.errors import CodeCheckError

NAME = "resolve"
HELP = "Resolve a thread whose fix is in a commit (refuses without a real commit)"


def add_arguments(p) -> None:
    p.add_argument("--pr", required=True)
    p.add_argument("--thread", required=True)
    p.add_argument("--commit", required=True, help="SHA of the commit that fixes the thread")


def run(args, ctx) -> dict:
    if not ctx.config.address.resolve_fixed_threads:
        return {"resolved": False, "reason": "disabled by address.resolve_fixed_threads"}
    if not ctx.git.ref_exists(args.commit):
        raise CodeCheckError(f"commit {args.commit} does not exist locally; only fixed threads may be resolved")
    ref = ctx.resolve_ref(args.pr)
    refspec, local_ref = ctx.provider.fetch_refspec(ref)
    if not ctx.git.fetch(ctx.config.remote, refspec):
        raise CodeCheckError(f"could not fetch the head of {ref} to verify the fix commit")
    if not ctx.git.is_ancestor(args.commit, local_ref):
        return {"resolved": False, "reason": f"commit {args.commit} is not on the PR head; push it first"}
    thread = find_thread(ctx, ref, args.thread)
    ctx.provider.resolve(thread)
    return {"resolved": True}
