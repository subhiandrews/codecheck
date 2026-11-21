import time

from codecheck.config import expand_path
from codecheck.memory import cli_lessons
from codecheck.memory.consolidate import Params
from codecheck.memory.db import MemoryDB
from codecheck.memory.measure import measure
from codecheck.memory.pipeline import consolidate_store
from codecheck.shadow.store import ShadowStore

NAME = "memory"
HELP = "Review memory: measure recurrence (read-only) or consolidate the shadow store into the lesson store"

clock = time.time  # tests replace this


def add_arguments(p) -> None:
    sub = p.add_subparsers(dest="memory_cmd", required=True)
    sub.add_parser("measure", help="temporal leave-one-out over the shadow store; prints counts, writes nothing")
    c = sub.add_parser("consolidate", help="turn the shadow store's evidence into lessons in memory.store; idempotent")
    c.add_argument("--prune", action="store_true", help="also delete evidence older than memory.expire_days")
    cli_lessons.add_arguments(sub)


def run(args, ctx) -> dict:
    if args.memory_cmd in cli_lessons.COMMANDS:
        return cli_lessons.run(args, ctx)
    # Both read the shadow store directly, available with shadow recording off. A missing store is empty.
    store = ctx.shadow or ShadowStore(expand_path(ctx.config.shadow.store))
    if args.memory_cmd == "measure":
        return measure(store)
    # Explicit and user-invoked, so it works with memory.enabled = false. Nothing in a review reads this store yet.
    m = ctx.config.memory
    params = Params(half_days=float(m.half_life_days), expire_days=float(m.expire_days))
    now = clock()
    return consolidate_store(store, MemoryDB(expand_path(m.store), now=lambda: now), params, now, prune=args.prune)
