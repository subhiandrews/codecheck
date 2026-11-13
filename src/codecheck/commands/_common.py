import json
from pathlib import Path

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


def contained(root: Path | str, rel: str) -> Path | None:
    """Resolve `rel` under `root`; None if it is absolute or escapes (via `..` or a symlink)."""
    if not rel or Path(rel).is_absolute() or "\0" in rel:
        return None
    base = Path(root).resolve()
    target = (base / rel).resolve()
    return target if target.is_relative_to(base) else None


def first_line(root: Path, rel: str) -> str:
    target = contained(root, rel)
    if target is None:
        return ""
    try:
        with open(target, encoding="utf-8", errors="replace") as fh:
            return fh.readline().strip()
    except OSError:
        return ""


def check_finding_paths(findings, errors, root, who):
    """Drop findings whose (untrusted) file path escapes the repo root; report each as an error."""
    kept = []
    errors = list(errors)
    for f in findings:
        if contained(root, f.file) is None:
            errors.append(f"{who}: finding path {f.file!r} escapes the repository")
        else:
            kept.append(f)
    return kept, errors


def finding_items(raw):
    return raw["findings"] if isinstance(raw, dict) and "findings" in raw else raw
