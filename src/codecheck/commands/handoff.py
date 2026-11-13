"""Write a run's hand-off files (and raw outputs) to the shadow store. Invisible unless `shadow.handoff` is on."""

import argparse
import json
from pathlib import Path

from codecheck import __version__
from codecheck.commands._common import (
    check_finding_paths,
    finding_items,
    load_meta,
    read_json,
)
from codecheck.core.diffmap import parse_diff
from codecheck.core.findings import (
    attach_anchors,
    finding_id,
    merge_exact_duplicates,
    validate_findings,
)
from codecheck.models import PRRef
from codecheck.runids import check_run_id
from codecheck.shadow.collect import collect_outcomes
from codecheck.shadow.handoff import SCHEMA, build_comparison, render_markdown
from codecheck.shadow.scrub import scrub, scrub_text
from codecheck.shadow.store import write_private
from codecheck.shadow.transcripts import (
    copy_scrubbed,
    extract_signals,
    find_transcript,
    subagent_transcripts,
)

NAME = "handoff"
HELP = argparse.SUPPRESS  # an internal skill step; hidden from `code-check --help`


def add_arguments(p) -> None:
    p.add_argument("--run", required=True)


def run(args, ctx) -> dict:
    if not ctx.config.shadow.handoff or ctx.shadow is None:
        return {"handoff": False}  # no files, no provider calls: a run with the flag off shows no sign of hand-off
    try:
        return build_handoff(ctx, check_run_id(args.run))
    except Exception as e:  # noqa: BLE001 - hand-off must never fail a review
        return {"handoff": False, "error": scrub_text(f"{type(e).__name__}: {e}")[:500]}


def build_handoff(ctx, run_id: str) -> dict:
    meta = load_meta(ctx, run_id)  # the run directory is gone after `cleanup`, so this runs before it
    run_dir = ctx.run_dir(run_id)
    ingest = read_json(run_dir / "ingest.json") if (run_dir / "ingest.json").exists() else None
    reviewer_raw, aggregator_raw = _raw_outputs(run_dir, meta)
    ctx.shadow.save_raw(run_id, {
        "reviewers": reviewer_raw, "panel": meta.get("panel"), "chunks": meta.get("chunks"), "base_ref": meta.get("base_ref"),
        **({"aggregator": aggregator_raw} if aggregator_raw is not None else {}),
    })
    folder = ctx.shadow.handoff_dir(run_id)
    transcript, transcript_reason, transcript_files, signals = _transcripts(ctx, run_id, folder)
    records = ctx.shadow.read(run_id)
    decision = next((r for r in reversed(records) if r.get("type") == "decision"), None)
    pr_url = comparison = None
    if meta.get("mode") != "pr" or not meta.get("pr"):
        comparison_reason = "local review: there is no pull request to compare with"
    else:
        ref = PRRef.parse(meta["pr"])
        pr_url = _pr_url(ctx, ref)
        if decision is None:
            comparison_reason = "review was not posted (draft, stale or dry run), so there is nothing to compare"
        else:
            comparison, comparison_reason = _compare(ctx, ref, run_id)
    handoff = {
        "schema": SCHEMA, "run_id": run_id, "skill": meta.get("skill"), "mode": meta.get("mode"), "repo": meta.get("repo"),
        "pr": meta.get("pr"), "pr_url": pr_url, "head_sha": meta.get("head_sha"), "tool_version": __version__,
        "models": meta.get("models"), "panel": meta.get("panel"), "blind": bool(meta.get("blind")),
        "reviewers": _reviewers(run_dir, meta, ingest), "signals": signals,
        "findings": _findings(run_dir, meta, aggregator_raw),
        "decision": {k: decision.get(k) for k in ("event", "reason", "incomplete")} if decision else None,
        "comparison": comparison, "comparison_reason": comparison_reason,
        "transcript": transcript, "transcript_reason": transcript_reason, "transcript_files": transcript_files,
    }
    handoff = scrub(handoff)  # structurally, so the JSON stays valid
    write_private(folder / "handoff.json", json.dumps(handoff, indent=2, ensure_ascii=False))
    write_private(folder / "handoff.md", scrub_text(render_markdown(handoff)))
    return {"handoff": True, "dir": str(folder), "pr_url": pr_url, "transcript": transcript,
            "comparison": True if comparison is not None else comparison_reason}


def _load(path: Path):
    if path.is_symlink() or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _raw_outputs(run_dir: Path, meta: dict) -> tuple[dict, object]:
    """Reviewer files as `ingest` left them, and the aggregator's file; missing or unreadable files are left out."""
    fdir = run_dir / "findings"
    reviewers = {}
    for member in (meta.get("panel") or {}).get("members", []):
        data = _load(fdir / f"{member}.json")
        if data is not None:
            reviewers[member] = data
    return reviewers, _load(fdir / "aggregator.json")


def _reviewers(run_dir: Path, meta: dict, ingest: dict | None) -> dict:
    out = {}
    for member in (meta.get("panel") or {}).get("members", []):
        if ingest is None:
            out[member] = {"output": (run_dir / "findings" / f"{member}.json").is_file(), "valid": None, "errors": []}
        else:
            out[member] = {"output": member not in ingest.get("missing", []), "valid": ingest.get("valid", {}).get(member),
                           "errors": list(ingest.get("invalid", {}).get(member, []))}
    return out


def _findings(run_dir: Path, meta: dict, raw) -> list[dict]:
    """The aggregated findings with the same ids `post` gives them (anchored against the diff, exact repeats merged)."""
    if raw is None:
        return []
    findings, errors = validate_findings(finding_items(raw), reviewer="aggregator", root=meta.get("root"))
    findings, _ = check_finding_paths(findings, errors, meta.get("root"), "aggregator")
    diff = run_dir / "diff.patch"
    files = parse_diff(diff.read_text(encoding="utf-8")) if diff.exists() else {}
    merged = merge_exact_duplicates(attach_anchors(findings, files))
    return [{"id": finding_id(f), "file": f.file, "line": f.line, "severity": f.severity.value, "category": f.category}
            for f in merged]


def _projects_dir(ctx) -> Path:
    base = ctx.env.get("CLAUDE_CONFIG_DIR")
    return (Path(base).expanduser() if base else Path.home() / ".claude") / "projects"


def _transcripts(ctx, run_id: str, folder: Path) -> tuple[bool, str | None, list[str], dict | None]:
    main = find_transcript(_projects_dir(ctx), run_id)
    if main is None:
        return False, "no Claude Code transcript from the last day mentions this run id", [], None
    paths = [main, *subagent_transcripts(main)]
    # Copies are regex-scrubbed raw JSONL; a scrub can occasionally break a line, so they are for reading, not parsing.
    copied = copy_scrubbed(paths, folder / "transcript")
    return True, None, copied, extract_signals(paths)


def _compare(ctx, ref: PRRef, run_id: str) -> tuple[dict | None, str | None]:
    """Record human comments the way `shadow collect` does, then compare them with this run's posted findings."""
    try:
        collect_outcomes(ctx.provider, ctx.shadow, ref, ctx.provider.whoami())
    except Exception as e:  # noqa: BLE001 - offline or a gh failure: keep the rest of the hand-off
        return None, scrub_text(f"comparison failed: {type(e).__name__}: {e}")[:500]
    records = ctx.shadow.read(run_id)
    return build_comparison(records, [r for r in records if r.get("type") == "finding"]), None


def _pr_url(ctx, ref: PRRef) -> str | None:
    try:
        return ctx.provider.get_pr(ref).url or None
    except Exception:  # noqa: BLE001 - the link is a convenience; the hand-off is still useful without it
        return None
