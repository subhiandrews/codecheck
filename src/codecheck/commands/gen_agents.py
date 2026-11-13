from pathlib import Path

from codecheck.agentgen import render_all
from codecheck.errors import CodeCheckError

NAME = "gen-agents"
HELP = "Write agent definitions with the models from the effective config"


def add_arguments(p) -> None:
    p.add_argument("--out", required=True, help="directory to write agents into")


def run(args, ctx) -> dict:
    if args.out.startswith("-"):
        raise CodeCheckError("--out must not start with '-'")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    files = render_all(ctx.config.models)
    for name, text in files.items():
        (out / name).write_text(text, encoding="utf-8")
    return {"written": sorted(files), "out": str(out)}
