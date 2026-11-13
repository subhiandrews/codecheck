import json
import shutil

from codecheck.commands._common import (
    check_finding_paths,
    finding_items,
    load_meta,
    write_json,
)
from codecheck.core.findings import validate_findings

NAME = "ingest"
HELP = "Validate the findings files the reviewers wrote for a run"


def add_arguments(p) -> None:
    p.add_argument("--run", required=True)


def run(args, ctx) -> dict:
    meta = load_meta(ctx, args.run)
    fdir = ctx.run_dir(args.run) / "findings"
    valid: dict[str, int] = {}
    invalid: dict[str, list[str]] = {}
    missing: list[str] = []
    for member in meta["panel"]["members"]:
        path = fdir / f"{member}.json"
        if path.is_symlink():
            path.unlink()
            invalid[member] = [f"{member}: findings file was a symlink; rejected"]
            continue
        if not path.exists():
            missing.append(member)
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            (fdir / "invalid").mkdir(exist_ok=True)
            shutil.move(path, fdir / "invalid" / path.name)
            invalid[member] = [f"{member}: not valid JSON ({e})"]
            continue
        findings, errors = validate_findings(finding_items(raw), reviewer=member, root=meta["root"])
        findings, errors = check_finding_paths(findings, errors, meta["root"], member)
        path.unlink()  # never write through a symlink planted by a reviewer
        path.write_text(json.dumps([f.to_dict() for f in findings], indent=2), encoding="utf-8")
        valid[member] = len(findings)
        if errors:
            invalid[member] = errors
    result = {"valid": valid, "invalid": invalid, "missing": missing, "incomplete": bool(invalid or missing)}
    write_json(ctx.run_dir(args.run) / "ingest.json", result)
    return result
