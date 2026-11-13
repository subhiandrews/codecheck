import os
from pathlib import Path

from codecheck.commands._common import contained
from codecheck.errors import CodeCheckError
from codecheck.vcs.git import Git

NAME = "commit"
HELP = "Commit the given paths (explicit paths only)"


def add_arguments(p) -> None:
    p.add_argument("--message", required=True)
    p.add_argument("--paths", nargs="+", required=True)


def run(args, ctx) -> dict:
    root = ctx.git.toplevel().resolve()
    rel_paths = []
    for path in args.paths:
        # Paths are relative to the cwd (as the caller sees them); pass them to git relative to the toplevel.
        target = contained(root, os.path.relpath(Path(ctx.git.cwd).resolve() / path, root)) if path and not Path(path).is_absolute() else None
        if target is None:
            raise CodeCheckError(f"path {path!r} escapes the repository")
        rel_paths.append(str(target.relative_to(root)))
    return {"sha": Git(ctx.runner, root).commit(args.message, rel_paths)}
