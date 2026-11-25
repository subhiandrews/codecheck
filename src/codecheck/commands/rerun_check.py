from codecheck.errors import CodeCheckError, ProviderError

NAME = "rerun-check"
HELP = "Rerun a failing check that is not the PR's fault; says why when it could not"


def add_arguments(p) -> None:
    p.add_argument("--pr", required=True)
    p.add_argument("--check", required=True)


def run(args, ctx) -> dict:
    ref = ctx.resolve_ref(args.pr)
    for c in ctx.provider.get_checks(ref):
        if c.name != args.check:
            continue
        if c.state != "fail":
            return {"name": c.name, "rerun": False, "reason": f"check is not failing ({c.state})"}
        try:
            ctx.provider.rerun_check(ref, c)
        except ProviderError as e:  # a rerun that the host refuses (it has answered HTTP 500) is for the skill to report
            return {"name": c.name, "rerun": False, "reason": str(e)}
        return {"name": c.name, "rerun": True}
    raise CodeCheckError(f"no check named {args.check!r} on {ref}")
