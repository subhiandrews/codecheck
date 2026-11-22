from codecheck.commands._common import approval_skip, pr_activity
from codecheck.core.precheck import review_verdict
from codecheck.core.state import WorkItem, babysit_work, loop_key, select_work

NAME = "list-work"
HELP = "List PRs with something new to do (zero LLM tokens); capped per iteration"


def add_arguments(p) -> None:
    p.add_argument("--mode", choices=("review", "babysit"), required=True)
    p.add_argument("--repos", default="", help="comma-separated OWNER/REPO list (default: config loop.repos)")


def run(args, ctx) -> dict:
    cfg = ctx.config
    repos = [r.strip() for r in args.repos.split(",") if r.strip()] or list(cfg.loop.repos)
    me = ctx.provider.whoami()
    review = args.mode == "review"
    if review and not repos:
        # no queue: review the open PRs of the repository this runs in
        repos = [ctx.current_repo()]  # raises a clear error outside a clone of a known repository
    limit = cfg.loop.max_review_prs_per_iteration if review else cfg.loop.max_prs_per_iteration
    items: list[WorkItem] = []
    skipped: list[dict] = []
    refs = ctx.provider.list_prs("open" if review else "authored", repos)  # newest first, so the newest is reviewed first
    for ref in refs:
        if sum(ctx.state.get(loop_key(args.mode, i.ref)) != i.fingerprint for i in items) >= limit:
            break  # enough new work; older PRs wait for the next iteration and cost no API calls now
        try:
            item, skip = _consider(ctx, args.mode, ref, me)
        except Exception as e:  # noqa: BLE001 - one broken PR must not abort the whole listing
            item, skip = None, f"error: {type(e).__name__}: {e}"[:300]
        if skip:
            skipped.append({"pr": str(ref), "reason": skip})
        if item:
            items.append(item)
    chosen = select_work(items, ctx.state, args.mode, limit)
    return {"items": [{"pr": str(i.ref), "fingerprint": i.fingerprint, "reason": i.reason} for i in chosen],
            "considered": len(refs), "skipped": skipped}


def _consider(ctx, mode: str, ref, me: str) -> tuple[WorkItem | None, str | None]:
    pr = ctx.provider.get_pr(ref)
    if mode == "review":
        if pr.state != "open" or pr.is_draft or pr.author == me:
            return None, None
        if pr.is_cross_repo and not ctx.config.loop.review_forks:
            return None, "fork-based PR: not reviewed unattended (set loop.review_forks = true to include forks)"
        verdict = review_verdict(pr, ctx.state, pr_activity(ctx, ref, pr, me))
        if verdict.kind == "replies":
            return None, verdict.reason
        if verdict.kind == "reviewed":
            return None, None  # this head was already reviewed (by the loop or by hand)
        # Only a PR that would become work costs the extra `list_reviews` call of the approval check.
        approved = approval_skip(ctx, ref, pr)
        if approved:
            return None, approved
        return WorkItem(ref, pr.head_sha, "new head"), None
    if pr.is_cross_repo:
        return None, "fork-based PR: cannot push in Phase 1"
    return babysit_work(pr, ctx.provider.list_threads(ref), ctx.provider.get_checks(ref), me), None
