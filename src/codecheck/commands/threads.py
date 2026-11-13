from codecheck.core.state import actionable_threads
from codecheck.runids import new_run_id
from codecheck.shadow.recorder import record_run

NAME = "threads"
HELP = "List actionable review threads (unresolved, not outdated, someone else has the last word)"


def add_arguments(p) -> None:
    p.add_argument("--pr", required=True)


def run(args, ctx) -> dict:
    ref = ctx.resolve_ref(args.pr)
    me = ctx.provider.whoami()
    pr = ctx.provider.get_pr(ref)
    threads = actionable_threads(ctx.provider.list_threads(ref), me)
    reviewers = sorted({c.author for t in threads for c in t.comments if c.author != me and not c.author.endswith("[bot]")})
    run_id = new_run_id()
    if ctx.shadow is not None:
        ctx.guarded(lambda: record_run(
            ctx.shadow, {"run_id": run_id, "skill": "address-comments", "repo": ref.repo, "pr": str(ref),
                         "head_sha": pr.head_sha, "mode": "pr", "panel": {"members": []}}, "", ctx.config))
    return {
        "run_id": run_id, "pr": str(ref), "head_sha": pr.head_sha, "head_ref": pr.head_ref, "reviewers": reviewers,
        "threads": [
            {"id": t.id, "path": t.path, "line": t.line,
             "comments": [{"id": c.id, "author": c.author, "body": c.body, "created_at": c.created_at} for c in t.comments]}
            for t in threads
        ],
    }
