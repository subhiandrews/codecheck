from codecheck.commands._common import contained
from codecheck.errors import CodeCheckError

NAME = "commit"
HELP = "Commit the given paths (explicit paths only)"


def add_arguments(p) -> None:
    p.add_argument("--message", required=True)
    p.add_argument("--paths", nargs="+", required=True)


def run(args, ctx) -> dict:
    root = ctx.git.toplevel()
    for path in args.paths:
        if contained(root, path) is None:
            raise CodeCheckError(f"path {path!r} escapes the repository")
    return {"sha": ctx.git.commit(args.message, args.paths)}
