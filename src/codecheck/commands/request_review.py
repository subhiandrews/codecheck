NAME = "request-review"
HELP = "Re-request review from the given users"


def add_arguments(p) -> None:
    p.add_argument("--pr", required=True)
    p.add_argument("--users", required=True, help="comma-separated logins")


def run(args, ctx) -> dict:
    users = [u.strip() for u in args.users.split(",") if u.strip()]
    ctx.provider.request_review(ctx.resolve_ref(args.pr), users)
    return {"requested": users}
