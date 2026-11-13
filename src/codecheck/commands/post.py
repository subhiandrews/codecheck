import json

from codecheck.commands._common import (
    check_finding_paths,
    finding_items,
    load_meta,
    read_json,
)
from codecheck.core.diffmap import parse_diff
from codecheck.core.findings import finding_id, validate_findings
from codecheck.core.pipeline import finalize_findings, plan_review
from codecheck.core.policy import decide
from codecheck.core.render import (
    FINDING_MARKER,
    comment_body,
    render_local,
    summary_body,
)
from codecheck.errors import CodeCheckError, ProviderError
from codecheck.models import PRRef, ReviewComment
from codecheck.shadow.recorder import record_post

NAME = "post"
HELP = "Post the aggregated review (PR mode) or print it (local mode)"


def add_arguments(p) -> None:
    p.add_argument("--run", required=True)


def _reviewer_findings(run_dir, ingest, meta) -> dict:
    out = {}
    for member in ingest["valid"]:
        raw = json.loads((run_dir / "findings" / f"{member}.json").read_text(encoding="utf-8"))
        items, errs = validate_findings(finding_items(raw), reviewer=member, root=meta["root"])
        out[member], _ = check_finding_paths(items, errs, meta["root"], member)
    return out


def _already_posted(threads, me: str) -> set[str]:
    """Finding IDs whose hidden marker is already in one of our own inline comments on this PR."""
    return {fid for t in threads for c in t.comments if c.author == me for fid in FINDING_MARKER.findall(c.body)}


def run(args, ctx) -> dict:
    cfg = ctx.config
    meta = load_meta(ctx, args.run)
    run_dir = ctx.run_dir(args.run)
    if not (run_dir / "ingest.json").exists():
        raise CodeCheckError("run `code-check ingest` before `post`")
    ingest = read_json(run_dir / "ingest.json")
    agg_path = run_dir / "findings" / "aggregator.json"
    if not agg_path.exists():
        raise CodeCheckError("aggregator output missing: findings/aggregator.json")
    try:
        raw = json.loads(agg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise CodeCheckError(f"aggregator output is not valid JSON: {e}") from e
    dropped = raw.get("dropped", []) if isinstance(raw, dict) else []
    findings, errors = validate_findings(finding_items(raw), reviewer="aggregator", root=meta["root"])
    findings, errors = check_finding_paths(findings, errors, meta["root"], "aggregator")
    if errors:
        raise CodeCheckError("aggregator output invalid: " + "; ".join(errors[:5]))
    diff_files = parse_diff((run_dir / "diff.patch").read_text(encoding="utf-8"))
    findings = finalize_findings(findings, diff_files)
    failed = sorted([*ingest["missing"], *ingest["invalid"]])
    incomplete = bool(ingest["incomplete"])

    if meta["mode"] == "local":
        seen = set(ctx.state.get(f"seen:{meta['local_key']}", []))
        new = [f for f in findings if finding_id(f) not in seen]
        ctx.state.set(f"seen:{meta['local_key']}", sorted({finding_id(f) for f in findings}))
        if not incomplete:
            ctx.state.set(meta["local_key"], meta["fingerprint"])
        unchanged = len(findings) - len(new)
        return {"mode": "local", "new_count": len(new), "unchanged_count": unchanged, "incomplete": incomplete,
                "failed_reviewers": failed, "text": render_local(new, unchanged, meta.get("base_warning"))}

    ref = PRRef.parse(meta["pr"])
    pr = ctx.provider.get_pr(ref)
    me = ctx.provider.whoami()
    if pr.author == me:
        raise CodeCheckError("refusing to post a review on your own PR")
    if pr.is_draft:
        return {"posted": False, "draft": True, "reason": "PR is a draft; draft PRs are never posted to"}
    if pr.head_sha != meta["head_sha"]:
        return {"posted": False, "stale": True,
                "reason": f"PR head moved ({meta['head_sha'][:8]} -> {pr.head_sha[:8]}); re-run the review"}
    decision = decide(findings, auto_approve=cfg.review.auto_approve, is_draft=pr.is_draft, incomplete=incomplete)
    already = _already_posted(ctx.provider.list_threads(ref), me)
    plan = plan_review(findings, diff_files, already=already, cap=cfg.review.max_inline_comments)
    inline, not_inline, repeated = plan.inline, plan.not_inline, plan.repeated
    comments = [ReviewComment(a.path, a.line, a.side, comment_body(f, finding_id(f), relocated=a.relocated)) for f, a in inline]
    body = summary_body(meta["run_id"], findings, not_inline, decision,
                        base_warning=meta.get("base_warning"), failed_reviewers=failed,
                        attribution=cfg.review.attribution)
    result = ctx.provider.post_review(ref, pr.head_sha, body, comments, decision)
    if incomplete:
        ctx.state.set(f"review-incomplete:{ref}", pr.head_sha)
    else:
        ctx.state.delete(f"review-incomplete:{ref}")
        ctx.state.set(f"review:{ref}", pr.head_sha)
    labeled = False
    if cfg.review.label:
        try:
            ctx.provider.add_label(ref, cfg.review.label)
            labeled = True
        except ProviderError:
            pass  # the reviewer may lack triage rights; the review itself is already posted
    if ctx.shadow is not None:
        ctx.guarded(lambda: record_post(
            ctx.shadow, meta, reviewer_findings=_reviewer_findings(run_dir, ingest, meta), findings=findings,
            inline_ids={finding_id(f) for f, _ in inline}, decision=decision, incomplete=incomplete, dropped=dropped))
    return {"posted": True, "degraded": result.degraded, "inline": len(comments), "unlisted": len(not_inline),
            "already_posted": repeated, "labeled": labeled,
            "findings": len(findings), "decision": decision.event, "reason": decision.reason,
            "incomplete": incomplete, "failed_reviewers": failed, "base_warning": meta.get("base_warning"),
            "run_id": meta["run_id"]}
