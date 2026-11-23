import os
import stat
import sys
from pathlib import Path

from codecheck.commands._common import contained, safe_repo
from codecheck.config import expand_path
from codecheck.core.guidance import GUIDANCE_FILE, guidance_dirs, read_regular
from codecheck.core.learn import changed_lines, check_proposal, diff_text, gather
from codecheck.core.state import ensure_private_dir
from codecheck.errors import CodeCheckError
from codecheck.runids import check_run_id

NAME = "guidance"
HELP = "Evidence for improving CODE_CHECK.md from past reviews, and a checked proposal written to the working tree or as a patch"


def add_arguments(p) -> None:
    sub = p.add_subparsers(dest="guidance_cmd", required=True)
    e = sub.add_parser("evidence", help="what past reviews say about this repository's guidance (needs review.learn_guidance)")
    e.add_argument("--dir", help="repository-relative directory the guidance is for (default: the directory the command runs in)")
    e.add_argument("--base", help="read the current file from this git ref instead of the working tree")
    a = sub.add_parser("apply", help="check a proposed CODE_CHECK.md on stdin and write it to the working tree; never commits")
    a.add_argument("--dir", help="the same directory given to `evidence`")
    a.add_argument("--base", help="patch mode: propose against this git ref and write a patch, never the working tree (needs --run)")
    a.add_argument("--run", help="patch mode: the run that names the patch, written to <state_dir>/proposals/<run>.patch (needs --base)")
    a.add_argument("--digest", required=True, help="the `digest` that `evidence` printed")
    sub.metavar = "{evidence,apply}"


def _scope(ctx, arg: str | None) -> tuple[Path, str]:
    """(repository root, directory relative to it, "" for the root). Never outside the repository or through a symlink."""
    root = ctx.git.toplevel()
    if arg is None:
        try:
            arg = ctx.git.cwd.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            arg = "."
    rel = "" if arg in ("", ".") else arg.strip("/")
    if rel:
        dirs, _ = guidance_dirs([f"{rel}/x"])
        if not dirs or dirs[-1] != rel or contained(root, rel) is None:
            raise CodeCheckError(f"{arg!r} is not a directory inside the repository")
    return root, rel


def _target(root: Path, rel: str) -> Path:
    return root / rel / GUIDANCE_FILE if rel else root / GUIDANCE_FILE


def _current(root: Path, rel: str) -> str | None:
    """The file's text, None when there is none. A symlink, a non-text file or a symlinked directory is an error: we
    must neither read nor replace it."""
    target = _target(root, rel)
    try:
        mode = os.lstat(target).st_mode
    except FileNotFoundError:
        if rel and not (root / rel).is_dir():
            raise CodeCheckError(f"{rel} is not a directory in this working tree") from None
        return None
    raw = read_regular(root, rel) if stat.S_ISREG(mode) else None
    if raw is None:
        raise CodeCheckError(f"{target} cannot be read safely (not a regular file, or behind a symlink); not touching it")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise CodeCheckError(f"{target} is not UTF-8 text; not touching it") from None


def _current_at(ctx, ref: str, rel: str) -> str | None:
    """The file's text at `ref`, None when nothing is there. A symlink, a submodule, a directory or a non-UTF-8 blob at
    that path is an error, never "missing": a proposal must not be made against something it cannot replace."""
    name = f"{rel}/{GUIDANCE_FILE}" if rel else GUIDANCE_FILE
    if not ctx.git.has_path(ref, name):
        return None
    text = ctx.git.show_file(ref, name)
    if text is None:
        raise CodeCheckError(f"{name} at {ref} is not a regular UTF-8 text file; not proposing against it")
    return text


def _run_dir(ctx, run_id: str) -> Path:
    path = expand_path(ctx.config.state_dir) / "runs" / check_run_id(run_id)
    if path.is_symlink() or not path.is_dir():
        raise CodeCheckError(f"there is no run directory for {run_id}")
    return path


def _write_patch(ctx, run_id: str, text: str) -> Path:
    """`<state_dir>/proposals/<run_id>.patch`. Not in the run directory: `cleanup` removes that when the review ends."""
    folder = expand_path(ctx.config.state_dir) / "proposals"
    if folder.is_symlink():
        raise CodeCheckError(f"{folder} is a symlink; not writing through it")
    target = ensure_private_dir(folder) / f"{run_id}.patch"
    _write_file(target, text)
    return target


def _write(root: Path, rel: str, text: str) -> None:
    _write_file(_target(root, rel), text)


def _write_file(target: Path, text: str) -> None:
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def run(args, ctx) -> dict:
    applying = args.guidance_cmd == "apply"
    if not ctx.config.review.learn_guidance:
        if applying:
            raise CodeCheckError("guidance learning is off: set review.learn_guidance = true in your user config")
        return {"enabled": False}
    if ctx.shadow is None:
        if applying:
            raise CodeCheckError("guidance learning needs shadow recording (shadow.enabled)")
        return {"enabled": True, "proposable": False, "reason": "shadow recording is off (shadow.enabled)"}
    base = args.base
    if applying and bool(base) != bool(args.run):
        raise CodeCheckError("--base and --run go together: a patch is proposed against a ref and written into a run directory")
    if applying and args.run:
        _run_dir(ctx, args.run)  # must be a real run; its id names the patch
    root, rel = _scope(ctx, args.dir)
    repo = safe_repo(ctx)
    evidence = gather(ctx.shadow, repo, rel)
    state_key = f"guidance:{repo}:{rel}"
    name = f"{rel}/{GUIDANCE_FILE}" if rel else GUIDANCE_FILE
    current = _current_at(ctx, base, rel) if base else _current(root, rel)
    if not applying:
        return {"enabled": True, "repo": repo, "dir": rel, "path": name, "current": current,
                "changed": evidence["digest"] != ctx.state.get(state_key), **evidence}
    if not evidence["proposable"]:
        raise CodeCheckError("there is no evidence to base a proposal on")
    if evidence["digest"] != args.digest:
        raise CodeCheckError("the evidence changed since `guidance evidence` ran; run it again")
    proposal = sys.stdin.read().rstrip("\n") + "\n"
    problem = check_proposal(current, proposal)
    if problem:
        raise CodeCheckError(problem)
    if base:
        patch = _write_patch(ctx, args.run, diff_text(current, proposal, name))
        ctx.state.set(state_key, evidence["digest"])  # proposed, even if nobody opens the PR: it is not suggested twice
        return {"patch": str(patch), "created": current is None, "changed_lines": changed_lines(current, proposal),
                "diff": diff_text(current, proposal, name)}
    _write(root, rel, proposal)
    ctx.state.set(state_key, evidence["digest"])  # the same evidence is not proposed on again
    return {"written": name, "created": current is None, "changed_lines": changed_lines(current, proposal),
            "diff": diff_text(current, proposal, name)}
