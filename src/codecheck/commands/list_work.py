from dataclasses import replace

from codecheck.commands._common import approval_skip, busy_checkout, pr_activity
from codecheck.core.precheck import review_verdict
from codecheck.core.state import WorkItem, babysit_work, loop_key, select_work
from codecheck.errors import CodeCheckError
from codecheck.models import same_pr_author

BATCH = 3  # PRs asked of the host at a time
MAX_LISTED = 50  # most PRs of one repository looked at in an iteration

NAME = "list-work"
HELP = "List PRs with something new to do (zero LLM tokens); capped per iteration"


def add_arguments(p) -> None:
    p.add_argument("--mode", choices=("review", "babysit"), required=True)
    p.add_argument("--repos", default="", help="comma-separated OWNER/REPO list (default: config loop.repos, else the clones under the working directory)")


def run(args, ctx) -> dict:
    cfg = ctx.config
    review = args.mode == "review"
    named = [r.strip() for r in args.repos.split(",") if r.strip()] or list(cfg.loop.repos)
    unusable: dict[str, str] = {}
    clones = ctx.clones(errors=unusable)  # the clone at, or the clones directly under, the working directory
    if named:
        targets = [(repo, clones.get(repo)) for repo in named]
    elif clones:
        targets = list(clones.items())
    else:
        raise CodeCheckError("run this from a clone, or from a directory that holds clones, or set loop.repos")
    limit = cfg.loop.max_review_prs_per_iteration if review else cfg.loop.max_prs_per_iteration
    items: list[WorkItem] = []
    skipped: list[dict] = []
    skipped.extend({"pr": path, "reason": f"not a usable clone: {why}"} for path, why in unusable.items())
    considered = 0
    fresh = 0  # items not yet handled at their current fingerprint; one state read per item, not per check

    def enough() -> bool:
        return fresh >= limit

    for repo, clone in targets:
        if enough():
            break  # the other clones wait for the next iteration and cost no API calls now
        sub = clone or ctx
        try:
            me = sub.provider.whoami()
            refs = _newest_first(sub.provider, "open" if review else "authored", repo)
            while not enough() and (ref := next(refs, None)) is not None:  # the cap is checked before the next batch is asked for
                considered += 1
                try:
                    item, skip = _consider(sub, args.mode, ref, me)
                    if item and clone and not review and (why := busy_checkout(ctx, str(clone.cwd))):
                        item, skip = None, why
                except Exception as e:  # noqa: BLE001 - one broken PR must not abort the whole listing
                    item, skip = None, f"error: {type(e).__name__}: {e}"[:300]
                if skip:
                    skipped.append({"pr": str(ref), "reason": skip})
                if item:
                    items.append(replace(item, clone=str(clone.cwd)) if clone else item)
                    fresh += ctx.state.get(loop_key(args.mode, item.ref)) != item.fingerprint
        except Exception as e:  # noqa: BLE001 - one unreachable repository must not stop the others
            skipped.append({"pr": repo, "reason": f"error: {type(e).__name__}: {e}"[:300]})
    chosen = select_work(items, ctx.state, args.mode, limit)
    return {"items": [{"pr": str(i.ref), "fingerprint": i.fingerprint, "reason": i.reason, "clone": i.clone} for i in chosen],
            "considered": considered, "skipped": skipped}


def _newest_first(provider, scope: str, repo: str):
    """The repository's PRs newest first, asked for in growing batches so that no more is listed than is looked at."""
    asked = BATCH
    seen = 0
    while True:
        refs = provider.list_prs(scope, [repo], limit=asked)
        yield from refs[seen:]
        if len(refs) < asked or asked >= MAX_LISTED:
            return
        seen, asked = len(refs), min(asked + BATCH, MAX_LISTED)


def _consider(ctx, mode: str, ref, me: str) -> tuple[WorkItem | None, str | None]:
    pr = ctx.provider.get_pr(ref)
    if mode == "review":
        if pr.state != "open" or pr.is_draft or same_pr_author(pr.author, me):
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
