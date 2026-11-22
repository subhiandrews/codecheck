"""`memory collect`: the explicit form of the automatic collection that `cleanup` runs when `memory.collect` is on."""

from codecheck.memory.autocollect import collect_due

HELP = "collect outcomes for the reviewed PRs that are due (1, 3, 7, 14 days after review), then consolidate"


def add_arguments(p) -> None:
    p.add_argument("--all", action="store_true", help="ignore the schedule: collect every PR in the shadow store")
    p.add_argument("--dry-run", action="store_true", help="list what is due; collect and write nothing")


def run(args, ctx) -> dict:
    # User-invoked, so it works with memory.collect = false. The same 3-PR and 10-second caps apply.
    return collect_due(ctx, everything=args.all, dry_run=args.dry_run, fallback_store=True)
