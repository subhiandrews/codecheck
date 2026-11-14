"""Write a run's hand-off files (and raw outputs) to the shadow store. Invisible unless `shadow.handoff` is on."""

import argparse
import json
import time
from pathlib import Path

from codecheck import __version__
from codecheck.commands._common import (
    check_finding_paths,
    finding_items,
    load_meta,
    read_json,
)
from codecheck.core.diffmap import parse_diff
from codecheck.core.findings import finding_id, validate_findings
from codecheck.core.pipeline import finalize_findings
from codecheck.models import PRRef
from codecheck.runids import check_run_id
from codecheck.shadow.buildid import plugin_build
from codecheck.shadow.collect import collect_outcomes
from codecheck.shadow.handoff import (
    SCHEMA,
    build_comparison,
    build_general_comments,
    render_markdown,
)
from codecheck.shadow.recorder import config_hash
from codecheck.shadow.scrub import scrub, scrub_text
from codecheck.shadow.store import write_private
from codecheck.shadow.transcripts import (
    belongs_to_run,
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
        "root": meta.get("root"), **({"aggregator": aggregator_raw} if aggregator_raw is not None else {}),
    })
    folder = ctx.shadow.handoff_dir(run_id)
    records = ctx.shadow.read(run_id)
    started = next((r["ts"] for r in records if r.get("type") == "run" and isinstance(r.get("ts"), (int, float))), None)
    transcript, transcript_reason, transcript_files, signals = _transcripts(ctx, run_id, folder, started)
    ignored_guidance = signals.pop("ignored_guidance", []) if signals else []
    decision = next((r for r in reversed(records) if r.get("type") == "decision"), None)
    findings = _findings(run_dir, meta, aggregator_raw)
    pr_url = comparison = comparison_mode = head_moved = pr_state = general_reason = None
    if meta.get("mode") != "pr" or not meta.get("pr"):
        comparison_reason = "local review: there is no pull request to compare with"
    else:
        ref = PRRef.parse(meta["pr"])
        try:
            pr = ctx.provider.get_pr(ref)
        except Exception as e:  # noqa: BLE001 - the link and the unposted fields are conveniences
            pr = None
            fetch_reason = scrub_text(f"comparison failed: could not fetch the PR: {type(e).__name__}: {e}")[:500]
        pr_url = (pr.url or None) if pr is not None else None
        if decision is not None:  # the posted comparison does not need the PR object
            comparison_mode = "posted"
            comparison, comparison_reason, general_reason = _compare(ctx, ref, run_id)
        elif pr is None:
            comparison_reason = fetch_reason
        elif pr.is_draft:
            comparison_reason = "review was not posted (draft, stale or dry run), so there is nothing to compare"
        else:
            comparison_mode, pr_state = "unposted", pr.state
            head_moved = pr.head_sha != meta.get("head_sha")
            comparison, comparison_reason, general_reason = _compare(ctx, ref, run_id, findings=findings)
    handoff = {
        "schema": SCHEMA, "run_id": run_id, "skill": meta.get("skill"), "mode": meta.get("mode"), "repo": meta.get("repo"),
        "pr": meta.get("pr"), "pr_url": pr_url, "head_sha": meta.get("head_sha"), "tool_version": __version__,
        **plugin_build(ctx.env),
        "config_hash": config_hash(ctx.config), "models": meta.get("models"), "panel": meta.get("panel"), "blind": bool(meta.get("blind")),
        "reviewers": _reviewers(run_dir, meta, ingest), "signals": signals,
        "findings": findings, "dropped": _dropped(aggregator_raw),
        "ignored_guidance": ignored_guidance,
        "decision": {k: decision.get(k) for k in ("event", "reason", "incomplete")} if decision else None,
        "comparison": comparison, "comparison_reason": comparison_reason, "general_comments_reason": general_reason,
        "comparison_mode": comparison_mode, "head_moved": head_moved, "pr_state": pr_state,
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
    """The aggregated findings with the ids and order `post` gives them (the same `finalize_findings` step)."""
    if raw is None:
        return []
    findings, errors = validate_findings(finding_items(raw), reviewer="aggregator", root=meta.get("root"))
    findings, _ = check_finding_paths(findings, errors, meta.get("root"), "aggregator")
    diff = run_dir / "diff.patch"
    files = parse_diff(diff.read_text(encoding="utf-8")) if diff.exists() else {}
    return [{"id": finding_id(f), "file": f.file, "line": f.line, "severity": f.severity.value, "category": f.category}
            for f in finalize_findings(findings, files)]


def _dropped(raw) -> list:
    """The aggregator's own `dropped` list as it wrote it (the whole hand-off is scrubbed before it is written)."""
    dropped = raw.get("dropped") if isinstance(raw, dict) else None
    return list(dropped) if isinstance(dropped, list) else []


def _projects_dir(ctx) -> Path:
    base = ctx.env.get("CLAUDE_CONFIG_DIR")
    return (Path(base).expanduser() if base else Path.home() / ".claude") / "projects"


def _transcripts(ctx, run_id: str, folder: Path, started: float | None) -> tuple[bool, str | None, list[str], dict | None]:
    main = find_transcript(_projects_dir(ctx), run_id)
    if main is None:
        return False, "no Claude Code transcript from the last day mentions this run id", [], None
    # The main session may hold many runs and the subagents folder every run's agents: keep this run's subagents only.
    subs = [p for p in subagent_transcripts(main) if belongs_to_run(p, run_id)]
    # Copies are regex-scrubbed raw JSONL; a scrub can occasionally break a line, so they are for reading, not parsing.
    copied = copy_scrubbed([main, *subs], folder / "transcript")
    if started is None:
        return True, "main session not scoped: run start unknown", copied, extract_signals(subs)
    return True, None, copied, extract_signals([main, *subs], windows={main: (started, time.time())})


def _compare(ctx, ref: PRRef, run_id: str, *, findings: list[dict] | None = None) -> tuple[dict | None, str | None, str | None]:
    """Record human comments the way `shadow collect` does, then compare them with this run's findings.

    A posted run's findings are its shadow `finding` records; an unposted run has none, so the caller passes the
    hand-off's own findings. The third element is why the general comments were skipped, if they were."""
    try:
        collect_outcomes(ctx.provider, ctx.shadow, ref, ctx.provider.whoami())
    except Exception as e:  # noqa: BLE001 - offline or a gh failure: keep the rest of the hand-off
        return None, scrub_text(f"comparison failed: {type(e).__name__}: {e}")[:500], None
    ours = findings if findings is not None else [r for r in ctx.shadow.read(run_id) if r.get("type") == "finding"]
    # collect files a comment under the newest run whose findings it is near, so on a re-reviewed PR some of
    # this PR's comments live in older runs' records. Take every thread's latest record across the PR's runs
    # and match it against this run's findings, so a comment this run missed counts as a miss.
    comments = sorted(
        (r for rid in ctx.shadow.runs_for_pr(str(ref)) for r in ctx.shadow.read(rid) if r.get("type") == "human_comment"),
        key=lambda r: r.get("ts") if isinstance(r.get("ts"), (int, float)) else 0,
    )
    rematched = [{**r, "matches_finding": _near(ours, r.get("path"), r.get("line"))} for r in comments]
    general, general_reason = _general_comments(ctx, ref)
    return build_comparison(rematched, ours, general), None, general_reason


def _general_comments(ctx, ref: PRRef) -> tuple[list[dict], str | None]:
    """The PR's review summaries and conversation comments, and why there are none when fetching failed."""
    step = "list_reviews"
    try:
        reviews = ctx.provider.list_reviews(ref)
        step = "list_issue_comments"
        issue_comments = ctx.provider.list_issue_comments(ref)
        step = "get_pr"
        general = build_general_comments(reviews, issue_comments, pr_author=ctx.provider.get_pr(ref).author)
        return [{**g, "body": " ".join(scrub_text(g["body"]).split())[:300]} for g in general], None
    except Exception as e:  # noqa: BLE001 - the extra feedback is a convenience; the rest of the comparison stands
        return [], scrub_text(f"{step} failed: {type(e).__name__}")[:500]


def _near(findings: list[dict], path, line) -> str | None:
    """The same rule as `shadow collect`: same file, line within 3; first finding wins."""
    if not isinstance(line, int):
        return None
    return next((f.get("id") for f in findings if f.get("file") == path and isinstance(f.get("line"), int)
                 and abs(f["line"] - line) <= 3), None)
