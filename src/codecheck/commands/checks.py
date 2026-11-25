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
        # once per name: a workflow that ran for two events (pull_request and push) is two checks with one name, and the
        # attempt counter is per name, so listing it twice spent two attempts on one failure (#106)
        "failing": list(dict.fromkeys(c.name for c in checks if c.state == "fail")),
        "attempts": attempts,
        "max_attempts": ctx.config.address.max_ci_attempts,
    }
