from argparse import Namespace

from codecheck.commands import list_work
from codecheck.commands.run_pr import run_one
from codecheck.config import expand_path
from codecheck.core.state import FileLock, WorkItem, mark_processed
from codecheck.models import PRRef

NAME = "loop"
HELP = "One loop iteration: find PRs with new work, run each in its own headless process, one at a time"


def add_arguments(p) -> None:
    p.add_argument("--mode", choices=("review", "babysit"), required=True)
    p.add_argument("--repos", default="")
    p.add_argument("--dry-run", action="store_true")


def run(args, ctx) -> dict:
    lock = FileLock(expand_path(ctx.config.state_dir) / "locks" / f"{args.mode}.lock")
    if not lock.acquire():
        return {"skipped": True, "reason": "previous iteration still running"}
    try:
        work = list_work.run(Namespace(mode=args.mode, repos=args.repos), ctx)
        results = []
        for item in work["items"]:
            ref = PRRef.parse(item["pr"])
            try:
                results.append(run_one(ctx, args.mode, ref, item["fingerprint"], args.dry_run))
            except Exception as e:  # one broken PR must never stop the rest of the iteration
                if not args.dry_run:
                    mark_processed(ctx.state, args.mode, WorkItem(ref, item["fingerprint"], ""))
                results.append({"pr": item["pr"], "ok": False, "summary": f"error: {e}"})
        return {"mode": args.mode, "processed": len(results), "considered": work["considered"],
                "skipped": work["skipped"], "results": results}
    finally:
        lock.release()
