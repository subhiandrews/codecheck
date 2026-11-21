import time

from codecheck.commands import memory_collect
from codecheck.config import expand_path
from codecheck.memory import cli_lessons
from codecheck.memory import cli_server
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
    memory_collect.add_arguments(sub.add_parser("collect", help=memory_collect.HELP))
    cli_server.add_arguments(sub)  # serve, token, sync, status (memory/cli_server.py)


def run(args, ctx) -> dict:
    if args.memory_cmd in cli_lessons.COMMANDS:
        return cli_lessons.run(args, ctx)
    if args.memory_cmd in cli_server.COMMANDS:
        return cli_server.run(args, ctx)
    # Both read the shadow store directly, available with shadow recording off. A missing store is empty.
    if args.memory_cmd == "collect":
        return memory_collect.run(args, ctx)
    store = ctx.shadow or ShadowStore(expand_path(ctx.config.shadow.store))
    if args.memory_cmd == "measure":
        return measure(store)
    # Explicit and user-invoked, so it works with memory.enabled = false. Nothing in a review reads this store yet.
    m = ctx.config.memory
    params = Params(half_days=float(m.half_life_days), expire_days=float(m.expire_days))
    now = clock()
    return consolidate_store(store, MemoryDB(expand_path(m.store), now=lambda: now), params, now, prune=args.prune)
