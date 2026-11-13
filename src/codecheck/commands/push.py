import shlex

from codecheck.commands._common import safe_repo
from codecheck.errors import CodeCheckError

NAME = "push"
HELP = "Run the configured test command, then push HEAD to the PR branch (never forced)"


def add_arguments(p) -> None:
    p.add_argument("--pr", required=True)


def run(args, ctx) -> dict:
    cfg = ctx.config
    ref = ctx.resolve_ref(args.pr)
    pr = ctx.provider.get_pr(ref)
    if pr.is_cross_repo:
        raise CodeCheckError("fork-based PR: pushing to the contributor's fork is not supported in Phase 1")
    if pr.state != "open":
        return {"pushed": False, "reason": f"PR is {pr.state}; not pushing"}
    if safe_repo(ctx) != ref.repo:
        return {"pushed": False, "reason": f"this checkout is not {ref.repo}; use `workspace` first"}
    branch = ctx.git.current_branch()
    if branch not in (pr.head_ref, f"code-check/pr-{ref.number}"):
        return {"pushed": False, "reason": f"checked-out branch {branch!r} is not the PR branch; use `workspace` first"}
    if ctx.git.is_dirty():
        raise CodeCheckError("uncommitted changes present; commit or discard them before pushing")
    tests_passed = None
    if cfg.address.test_command:
        res = ctx.runner.run(shlex.split(cfg.address.test_command), cwd=ctx.git.cwd, timeout=1800)
        tests_passed = res.returncode == 0
        if not tests_passed:
            return {"pushed": False, "tests_passed": False, "output": (res.stdout + res.stderr)[-4000:]}
    refspec, local_ref = ctx.provider.fetch_refspec(ref)
    if not ctx.git.fetch(cfg.remote, refspec):
        raise CodeCheckError(f"could not fetch the head of {ref} to check for new commits")
    if not ctx.git.is_ancestor(local_ref, "HEAD"):
        return {"pushed": False, "tests_passed": tests_passed,
                "reason": "the remote branch has new commits; fetch and rebase or merge them first"}
    ctx.git.push(cfg.remote, pr.head_ref)
    return {"pushed": True, "tests_passed": tests_passed, "head_sha": ctx.git.head_sha()}
