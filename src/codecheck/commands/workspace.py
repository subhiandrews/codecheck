import shutil
from pathlib import Path

from codecheck.commands._common import contained, safe_repo
from codecheck.config import expand_path
from codecheck.errors import CodeCheckError
from codecheck.vcs.git import Git

NAME = "workspace"
HELP = "Return a safe directory to edit the PR branch in (in place, or an isolated worktree)"


def add_arguments(p) -> None:
    p.add_argument("--pr", required=True)


def _managed_clone(ctx, repo: str) -> Git:
    dest = expand_path(ctx.config.state_dir) / "repos" / repo.replace("/", "__")
    if contained(dest.parent, dest.name) is None:
        raise CodeCheckError(f"unsafe repository name {repo!r}")
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        res = ctx.runner.run(ctx.provider.clone_argv(repo, str(dest)), cwd=ctx.cwd)
        if res.returncode != 0:
            raise CodeCheckError(f"could not clone {repo}: {res.stderr.strip()[:300]}")
    return Git(ctx.runner, dest)


def run(args, ctx) -> dict:
    cfg = ctx.config
    ref = ctx.resolve_ref(args.pr)
    pr = ctx.provider.get_pr(ref)
    if pr.is_cross_repo:
        raise CodeCheckError("fork-based PR: pushing to the contributor's fork is not supported in Phase 1")
    if pr.state != "open":
        raise CodeCheckError(f"PR is {pr.state}")
    here = safe_repo(ctx)
    if here == ref.repo and ctx.git.current_branch() == pr.head_ref:
        if ctx.git.is_dirty():
            raise CodeCheckError("working tree has uncommitted changes; commit or stash them first")
        return {"path": str(ctx.git.cwd), "created": False}
    base = ctx.git if here == ref.repo else _managed_clone(ctx, ref.repo)
    refspec, local_ref = ctx.provider.fetch_refspec(ref)
    if not base.fetch(cfg.remote, refspec):
        raise CodeCheckError(f"could not fetch the head of {ref}")
    state_dir = expand_path(cfg.state_dir)
    name = f"addr-{ref.repo.replace('/', '__')}-{ref.number}"
    if contained(state_dir / "worktrees", name) is None:
        raise CodeCheckError(f"unsafe repository name {ref.repo!r}")
    path = state_dir / "worktrees" / name
    if path.exists():
        base.worktree_remove(path)
        shutil.rmtree(path, ignore_errors=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    base.worktree_add_branch(path, f"code-check/pr-{ref.number}", local_ref)
    return {"path": str(path), "created": True, "git_dir": str(Path(base.cwd))}
