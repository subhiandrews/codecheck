from codecheck.core.state import WorkItem, babysit_work, select_work

NAME = "list-work"
HELP = "List PRs with something new to do (zero LLM tokens); capped per iteration"


def add_arguments(p) -> None:
    p.add_argument("--mode", choices=("review", "babysit"), required=True)
    p.add_argument("--repos", default="", help="comma-separated OWNER/REPO list (default: config loop.repos)")


def run(args, ctx) -> dict:
    cfg = ctx.config
    repos = [r.strip() for r in args.repos.split(",") if r.strip()] or list(cfg.loop.repos)
    me = ctx.provider.whoami()
    scope = "review-requested" if args.mode == "review" else "authored"
    items: list[WorkItem] = []
    skipped: list[dict] = []
    refs = ctx.provider.list_prs(scope, repos)
    for ref in refs:
        pr = ctx.provider.get_pr(ref)
        if args.mode == "review":
            if pr.state == "open" and not pr.is_draft and pr.author != me:
                items.append(WorkItem(ref, pr.head_sha, "new head"))
            continue
        if pr.is_cross_repo:
            skipped.append({"pr": str(ref), "reason": "fork-based PR: cannot push in Phase 1"})
            continue
        item = babysit_work(pr, ctx.provider.list_threads(ref), ctx.provider.get_checks(ref), me)
        if item:
            items.append(item)
    chosen = select_work(items, ctx.state, args.mode, cfg.loop.max_prs_per_iteration)
    return {"items": [{"pr": str(i.ref), "fingerprint": i.fingerprint, "reason": i.reason} for i in chosen],
            "considered": len(refs), "skipped": skipped}
