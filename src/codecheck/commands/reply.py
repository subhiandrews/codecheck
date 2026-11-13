import sys

from codecheck.errors import CodeCheckError

NAME = "reply"
HELP = "Reply to a review thread (body on stdin)"
MARKER = "<!-- code-check:reply -->"


def add_arguments(p) -> None:
    p.add_argument("--pr", required=True)
    p.add_argument("--thread", required=True)


def find_thread(ctx, ref, thread_id: str):
    for t in ctx.provider.list_threads(ref):
        if t.id == thread_id:
            return t
    raise CodeCheckError(f"thread {thread_id} not found on {ref}")


def run(args, ctx) -> dict:
    ref = ctx.resolve_ref(args.pr)
    body = sys.stdin.read().strip()
    if not body:
        raise CodeCheckError("reply body (stdin) is empty")
    ctx.provider.reply(ref, find_thread(ctx, ref, args.thread), f"{body}\n\n{MARKER}")
    return {"replied": True}
