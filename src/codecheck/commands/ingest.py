import json
import shutil

from codecheck.commands._common import (
    check_finding_paths,
    finding_items,
    load_meta,
    note_feedback,
    write_json,
)
from codecheck.core.findings import validate_findings

NAME = "ingest"
HELP = "Validate the findings files the reviewers wrote for a run"


def add_arguments(p) -> None:
    p.add_argument("--run", required=True)


def _earlier_invalid(path) -> dict[str, list[str]]:
    """The `invalid` entries an earlier ingest wrote. Only lists of strings count: nothing else may reach post."""
    try:
        earlier = json.loads(path.read_text(encoding="utf-8")).get("invalid")
    except (OSError, ValueError, AttributeError):
        return {}
    if not isinstance(earlier, dict):
        return {}
    kept = {m: [e for e in errs if isinstance(e, str)] for m, errs in earlier.items() if isinstance(errs, list)}
    return {m: errs for m, errs in kept.items() if errs}


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
    ingest_path = ctx.run_dir(args.run) / "ingest.json"
    # The skills run ingest again after re-dispatching `missing` reviewers. Validation rewrites a findings file
    # without its bad items and quarantines unparseable ones, so a second run would forget they were invalid.
    for member, errors in _earlier_invalid(ingest_path).items():
        if member not in meta["panel"]["members"]:
            continue
        invalid[member] = errors + [e for e in invalid.get(member, []) if e not in errors]
        if member in missing:
            missing.remove(member)
    # A reviewer that wrote its findings under another name (#105) is `missing`; naming the file makes that visible.
    # `aggregator.json` is not exempt: ingest runs before the aggregator, and `post` reads that file as the review.
    expected = {f"{m}.json" for m in meta["panel"]["members"]}
    stray = sorted(p.name for p in fdir.glob("*.json") if p.name not in expected and p.is_file())
    result = {"valid": valid, "invalid": invalid, "missing": missing, "stray": stray, "incomplete": bool(invalid or missing)}
    write_json(ingest_path, result)
    if missing or stray:  # member names and a count, no run id or file names: the same failure in another run counts up
        note_feedback(ctx, "findings-missing", "ingest", missing=",".join(sorted(missing)), stray=len(stray))
    return result
