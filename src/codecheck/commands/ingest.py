import json
import os
import shutil

from codecheck.commands._common import (
    check_finding_paths,
    finding_items,
    load_meta,
    write_json,
)
from codecheck.core.findings import validate_findings
from codecheck.core.panel import detect_language
from codecheck.memory.inject import AGGREGATOR, FILE_NAME, safe_build
from codecheck.memory.semantic import Query

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
    candidates: list[tuple[str, str]] = []  # (file, category) of every valid finding, the key for finding lessons
    queries: list[Query] = []  # their messages, the text for similarity lookup (used only with memory.embed_model)
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
        candidates += [(f.file, f.category) for f in findings]
        queries += [Query(f.message, f.file, f.category, detect_language(f.file) or "") for f in findings]
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
    result = {"valid": valid, "invalid": invalid, "missing": missing, "incomplete": bool(invalid or missing)}
    publish = not missing or ingest_path.exists()  # never hand the aggregator a half-built input
    write_json(ingest_path, result)
    mem = _aggregator_memory(ctx, args.run, meta, candidates, queries if ctx.config.memory.embed_model else []) if publish else None
    if mem is not None and ctx.config.memory.enabled and ctx.config.memory.read:
        return {**result, "memory": mem}
    return result


def _aggregator_memory(ctx, run_id: str, meta: dict, candidates: list[tuple[str, str]], queries: list | None = None) -> dict | None:
    """Replace memory.md with the lessons for these findings and list it in context.json. None when memory is not in play.

    The reviewers' miss lessons are no use to the aggregator (it may not invent findings), so when nothing applies the
    old file is removed instead of left behind."""
    run_dir = ctx.run_dir(run_id)
    try:
        info = safe_build(ctx, run_dir, stage=AGGREGATOR, repo=meta.get("repo"), base_ref=meta.get("base_ref"),
                          blind=bool(meta.get("blind")), findings=candidates, queries=queries or ())
        if info["reason"] in ("disabled", "read off", "blind"):
            return None
        files = [n for n in meta.get("run_files", []) if n != FILE_NAME]
        if info.get("file"):
            files.append(FILE_NAME)
        else:
            (run_dir / FILE_NAME).unlink(missing_ok=True)
        if files != meta.get("run_files", []) or meta.get("memory_findings") != _brief(info):
            meta.update(run_files=files, memory_findings=_brief(info))
            stage = run_dir / "context.json.tmp"  # same directory, so the replace is atomic
            write_json(stage, meta)
            os.replace(stage, run_dir / "context.json")
        if info.get("read"):
            ctx.record(run_id, {"type": "memory", "stage": AGGREGATOR, "ids": info["ids"]})
        return _brief(info)
    except Exception as e:  # noqa: BLE001 - memory never breaks ingest
        return {"read": False, "reason": "error", "error": f"{type(e).__name__}: {e}"[:200]}


def _brief(info: dict) -> dict:
    return {k: v for k, v in info.items() if k != "ids"}
