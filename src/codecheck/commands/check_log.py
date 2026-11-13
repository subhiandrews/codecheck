from codecheck.errors import CodeCheckError

NAME = "check-log"
HELP = "Fetch the tail of a failing check's log"


def add_arguments(p) -> None:
    p.add_argument("--pr", required=True)
    p.add_argument("--check", required=True)


def run(args, ctx) -> dict:
    ref = ctx.resolve_ref(args.pr)
    for c in ctx.provider.get_checks(ref):
        if c.name == args.check:
            return {"name": c.name, "log": ctx.provider.get_check_log(ref, c)}
    raise CodeCheckError(f"no check named {args.check!r} on {ref}")
