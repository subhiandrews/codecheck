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
    thread = find_thread(ctx, ref, args.thread)
    ctx.provider.resolve(thread)
    return {"resolved": True}
