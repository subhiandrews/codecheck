from codecheck.vcs.sync import sync_base

NAME = "status"
HELP = "Report PR state and the local checkout's readiness (branch, dirty, behind, conflicts)"


def add_arguments(p) -> None:
    p.add_argument("--pr", required=True)


def run(args, ctx) -> dict:
    ref = ctx.resolve_ref(args.pr)
    pr = ctx.provider.get_pr(ref)
    sync = sync_base(ctx.git, ctx.config.remote, pr.base_ref)
    on_branch = ctx.git.current_branch() == pr.head_ref
    return {
        "pr": str(ref), "head_sha": pr.head_sha, "head_ref": pr.head_ref, "state": pr.state, "is_draft": pr.is_draft,
        "is_cross_repo": pr.is_cross_repo, "on_pr_branch": on_branch, "dirty": ctx.git.is_dirty(),
        "behind_base": ctx.git.behind_count(sync.base_ref) if on_branch else None,
        "conflicts_with_base": ctx.git.would_conflict(sync.base_ref) if on_branch else None,
        "local_matches_remote": (ctx.git.head_sha() == pr.head_sha) if on_branch else None,
        "base_warning": sync.warning,
        "can_push": pr.state == "open" and not pr.is_cross_repo,
    }
