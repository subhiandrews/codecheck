NAME = "attempt"
HELP = "Count a fix attempt for a failing check; says when attempts are exhausted"


def add_arguments(p) -> None:
    p.add_argument("--pr", required=True)
    p.add_argument("--check", required=True)


def run(args, ctx) -> dict:
    ref = ctx.resolve_ref(args.pr)
    n = ctx.state.incr(f"attempts:{ref}:{args.check}")
    return {"attempt": n, "exhausted": n > ctx.config.address.max_ci_attempts}
