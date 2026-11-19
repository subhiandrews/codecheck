from pathlib import Path

from codecheck.errors import CodeCheckError
from codecheck.skillgen import render_codex_skills

NAME = "gen-skills"
HELP = "Generate Codex skill templates from the checkout's Claude skills"


def add_arguments(p) -> None:
    p.add_argument("--out", required=True, help="directory to write Codex skills into")


def run(args, ctx) -> dict:
    if args.out.startswith("-"):
        raise CodeCheckError("--out must not start with '-'")
    files = render_codex_skills(ctx.cwd / "skills")
    out = Path(args.out)
    for relative, text in files.items():
        path = out / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return {"written": sorted(files), "out": str(out)}
