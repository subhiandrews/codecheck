NAME = "checks"
HELP = "List CI checks, failing ones, and retry attempts so far"


def add_arguments(p) -> None:
    p.add_argument("--pr", required=True)


def run(args, ctx) -> dict:
    ref = ctx.resolve_ref(args.pr)
    checks = ctx.provider.get_checks(ref)
    attempts = {}
    for c in checks:
        key = f"attempts:{ref}:{c.name}"
        if c.state == "pass":
            ctx.state.delete(key)
        elif c.state == "fail":
            attempts[c.name] = ctx.state.get(key, 0)
    return {
        "checks": [{"name": c.name, "state": c.state, "link": c.link} for c in checks],
        "failing": [c.name for c in checks if c.state == "fail"],
        "attempts": attempts,
        "max_attempts": ctx.config.address.max_ci_attempts,
    }
