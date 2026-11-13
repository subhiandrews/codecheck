import signal
import threading
from argparse import Namespace

from codecheck.commands import list_work
from codecheck.commands.run_pr import loop_bookkeeping, run_one
from codecheck.commands.workspace import CLONE_TIMEOUT_SECONDS
from codecheck.config import expand_path
from codecheck.core.state import FileLock, ensure_private_dir
from codecheck.models import PRRef

NAME = "loop"
HELP = "One loop iteration: find PRs with new work, run each in its own headless process, one at a time"

HEARTBEAT_SECONDS = 60
STALE_MARGIN_SECONDS = 900


def add_arguments(p) -> None:
    p.add_argument("--mode", choices=("review", "babysit"), required=True)
    p.add_argument("--repos", default="")
    p.add_argument("--dry-run", action="store_true")


def stale_after(config) -> int:
    """Longest a live iteration can legitimately take; a lock older than this (and not refreshed) is stale."""
    per_pr = config.run_pr.timeout_seconds + CLONE_TIMEOUT_SECONDS
    return config.loop.max_prs_per_iteration * per_pr + STALE_MARGIN_SECONDS


def _on_signal(signum, _frame):
    raise SystemExit(128 + signum)  # unwinds through `finally`, which releases the lock


def _install_signal_handlers() -> dict:
    if threading.current_thread() is not threading.main_thread():
        return {}
    old = {}
    for sig in (signal.SIGTERM, signal.SIGHUP):
        old[sig] = signal.signal(sig, _on_signal)
    return old


def run(args, ctx) -> dict:
    state_dir = ensure_private_dir(expand_path(ctx.config.state_dir))
    lock = FileLock(state_dir / "locks" / f"{args.mode}.lock", stale_after=stale_after(ctx.config))
    if not lock.acquire():
        return {"skipped": True, "reason": "previous iteration still running"}
    previous = _install_signal_handlers()
    try:
        with lock.keep_alive(HEARTBEAT_SECONDS):
            return _iterate(args, ctx, lock)
    finally:
        lock.release()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _iterate(args, ctx, lock: FileLock) -> dict:
    work = list_work.run(Namespace(mode=args.mode, repos=args.repos), ctx)
    results = []
    for item in work["items"]:
        lock.touch()
        ref = PRRef.parse(item["pr"])
        try:
            results.append(run_one(ctx, args.mode, ref, item["fingerprint"], args.dry_run))
        except Exception as e:  # one broken PR must never stop the rest of the iteration
            extra = {} if args.dry_run else loop_bookkeeping(ctx, args.mode, ref, item["fingerprint"], False)
            results.append({"pr": item["pr"], "ok": False, "summary": f"error: {e}", **extra})
    return {"mode": args.mode, "processed": len(results), "considered": work["considered"],
            "skipped": work["skipped"], "results": results}
