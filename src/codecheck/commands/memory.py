from codecheck.config import expand_path
from codecheck.memory.measure import measure
from codecheck.shadow.store import ShadowStore

NAME = "memory"
HELP = "Review memory: measure whether lessons from earlier runs would have applied to later ones (read-only)"


def add_arguments(p) -> None:
    sub = p.add_subparsers(dest="memory_cmd", required=True)
    sub.add_parser("measure", help="temporal leave-one-out over the shadow store; prints counts, writes nothing")


def run(args, ctx) -> dict:
    # Read-only and available with shadow recording off: open the configured store directly. A missing store is empty.
    store = ctx.shadow or ShadowStore(expand_path(ctx.config.shadow.store))
    return measure(store)
