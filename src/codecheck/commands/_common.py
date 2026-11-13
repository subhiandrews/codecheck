import json
from pathlib import Path

from codecheck.core.paths import check_finding_paths, contained, finding_items  # noqa: F401 - re-exported
from codecheck.core.precheck import approved_by, fetch_activity
from codecheck.errors import CodeCheckError


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_meta(ctx, run_id: str) -> dict:
    path = ctx.run_dir(run_id) / "context.json"
    if not path.exists():
        raise CodeCheckError(f"unknown run {run_id}; run `code-check context` first")
    return read_json(path)


def local_key(ctx) -> str:
    return f"local:{ctx.git.toplevel()}:{ctx.git.current_branch()}"


def safe_repo(ctx) -> str:
    try:
        return ctx.current_repo()
    except CodeCheckError:
        return ctx.git.cwd.name


def first_line(root: Path, rel: str) -> str:
    target = contained(root, rel)
    if target is None:
        return ""
    try:
        with open(target, encoding="utf-8", errors="replace") as fh:
            return fh.readline().strip()
    except OSError:
        return ""


def pr_activity(ctx, ref, pr, me: str | None = None):
    """Our latest review and replies on our threads, fetched only when they could change the verdict."""
    me = me or ctx.provider.whoami()
    return fetch_activity(pr, ctx.state, me, lambda: ctx.provider.list_reviews(ref),
                          lambda: ctx.provider.list_threads(ref, reactions=False))


def approval_skip(ctx, ref, pr) -> str | None:
    """Why to skip a PR that someone already approved at its current head, else None."""
    who = approved_by(ctx.provider.list_reviews(ref), pr.head_sha)
    return f"already approved by {who}" if who else None
