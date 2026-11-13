from codecheck.core.state import WorkItem, mark_processed

NAME = "mark-done"
HELP = "Record that a work item was processed (even if it failed) so it cannot starve the queue"


def add_arguments(p) -> None:
    p.add_argument("--mode", choices=("review", "babysit"), required=True)
    p.add_argument("--pr", required=True)
    p.add_argument("--fingerprint", required=True)


def run(args, ctx) -> dict:
    mark_processed(ctx.state, args.mode, WorkItem(ctx.resolve_ref(args.pr), args.fingerprint, ""))
    return {"marked": True}
