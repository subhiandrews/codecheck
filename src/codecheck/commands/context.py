import shutil

from codecheck.commands._common import first_line, local_key, safe_repo, write_json
from codecheck.config import expand_path, model_for
from codecheck.core.chunk import chunk_files
from codecheck.core.diffmap import parse_diff
from codecheck.core.guidance import from_base, from_worktree
from codecheck.core.panel import detect_language, filter_ignored, select_panel
from codecheck.core.precheck import cap_replies, review_activity
from codecheck.core.render import render_replies
from codecheck.core.state import ensure_private_dir, local_fingerprint
from codecheck.errors import CodeCheckError
from codecheck.runids import new_run_id
from codecheck.shadow.recorder import record_run
from codecheck.vcs.sync import sync_base

NAME = "context"
HELP = "Build the review bundle (diff, per-file patches, panel) for a run"


def add_arguments(p) -> None:
    p.add_argument("--mode", choices=("local", "pr"), required=True)
    p.add_argument("--pr")
    p.add_argument("--security", action="store_true", help="include the security reviewer")
    p.add_argument("--with-replies", action="store_true", help="PR mode: write replies.md with replies on our earlier threads")
    p.add_argument("--skill", default=None, help="recorded in shadow data")


def run(args, ctx) -> dict:
    run_id = new_run_id()
    ensure_private_dir(expand_path(ctx.config.state_dir))
    run_dir = ctx.run_dir(run_id)
    (run_dir / "findings").mkdir(parents=True)
    try:
        return _build(args, ctx, run_id, run_dir)
    except BaseException:
        shutil.rmtree(run_dir, ignore_errors=True)  # no half-built run without context.json
        raise


def _build(args, ctx, run_id, run_dir) -> dict:
    cfg = ctx.config
    if args.with_replies and args.mode != "pr":
        raise CodeCheckError("--with-replies requires --mode pr")
    replies = None
    meta: dict = {"run_id": run_id, "mode": args.mode, "skill": args.skill or ("review-local" if args.mode == "local" else "review-pr")}

    if args.mode == "local":
        sync = sync_base(ctx.git, cfg.remote, cfg.base_branch)
        merge_base = ctx.git.merge_base(sync.base_ref, "HEAD")
        diff = ctx.git.local_diff(merge_base)
        head = ctx.git.head_sha()
        root = ctx.git.toplevel()
        guidance = from_worktree(root)
        meta.update(root=str(root), head_sha=head, pr=None, repo=safe_repo(ctx), title=None, description=None,
                    fingerprint=local_fingerprint(merge_base, head, diff), local_key=local_key(ctx), worktree=False)
    else:
        if not args.pr:
            raise CodeCheckError("--pr is required for --mode pr")
        ref = ctx.resolve_ref(args.pr)
        if safe_repo(ctx) != ref.repo:
            raise CodeCheckError(f"run this from a clone of {ref.repo}")
        pr = ctx.provider.get_pr(ref)
        sync = sync_base(ctx.git, cfg.remote, pr.base_ref)
        refspec, local_ref = ctx.provider.fetch_refspec(ref)
        if not ctx.git.fetch(cfg.remote, refspec):
            raise CodeCheckError(f"could not fetch the head of {ref}")
        diff = ctx.git.diff(f"{sync.base_ref}...{local_ref}")
        if args.with_replies:
            act = review_activity(ctx.provider.list_reviews(ref), ctx.provider.list_threads(ref), ctx.provider.whoami())
            capped = cap_replies(act.replies)
            if capped:
                (run_dir / "replies.md").write_text(render_replies(capped), encoding="utf-8")
                replies = {"threads": len({r.thread_id for r in capped}), "comments": len(capped)}
        guidance = from_base(ctx.git, sync.base_ref)  # base branch only: a PR cannot change the rules it is judged by
        root = expand_path(cfg.state_dir) / "worktrees" / run_id
        root.parent.mkdir(parents=True, exist_ok=True)
        meta.update(root=str(root), head_sha=ctx.git.rev_parse(local_ref), pr=str(ref), repo=ref.repo,
                    title=pr.title, description=(pr.body or "")[:4000], fingerprint=None, local_key=None, worktree=True)

    parsed = parse_diff(diff)
    reviewable = filter_ignored(list(parsed), cfg.review.ignore)
    if not reviewable:
        raise CodeCheckError("no reviewable files; run `code-check precheck` first")
    if meta["worktree"]:
        ctx.git.worktree_add(root, local_ref)
    if guidance is not None:
        (run_dir / "CODE_CHECK.md").write_text(guidance.text, encoding="utf-8")
    meta["guidance"] = guidance.to_meta() if guidance else None
    meta["replies"] = replies
    (run_dir / "diff.patch").write_text(diff, encoding="utf-8")
    files = []
    for idx, path in enumerate(reviewable):
        rel = f"files/{idx:03d}.patch"
        (run_dir / rel).parent.mkdir(exist_ok=True)
        (run_dir / rel).write_text(parsed[path].patch, encoding="utf-8")
        files.append({"path": path, "status": parsed[path].status, "language": detect_language(path, first_line(root, path)),
                      "patch_file": rel, "chars": len(parsed[path].patch)})

    first_lines = {p: first_line(root, p) for p in reviewable}
    deep_cmd = cfg.deep_reviewer.command.split()
    deep_available = bool(cfg.deep_reviewer.enabled and deep_cmd and shutil.which(deep_cmd[0]))
    panel = select_panel(reviewable, first_lines, deep_enabled=cfg.deep_reviewer.enabled, deep_available=deep_available,
                         security=args.security or cfg.review.security)
    languages: dict[str, list[str]] = {}
    for f in files:
        if f["language"]:
            languages.setdefault(f["language"], []).append(f["path"])
    models = {m: model_for(cfg.models, "reviewers") for m in panel.core}
    models |= {f"lang-{lang}": model_for(cfg.models, "language_expert", lang) for lang in panel.languages}
    if panel.security:
        models["security"] = model_for(cfg.models, "security")
    if panel.deep:
        models["deep-reviewer"] = model_for(cfg.models, "deep")
    chunks = chunk_files({f["path"]: f["chars"] for f in files}, max_files=cfg.review.max_files,
                         max_chars=cfg.review.max_chunk_chars)
    meta.update(base_ref=sync.base_ref, base_warning=sync.warning, files=files, chunks=chunks, models=models,
                panel={"members": list(panel.members), "languages": languages, "notes": list(panel.notes)})
    write_json(run_dir / "context.json", meta)
    if ctx.shadow is not None:
        ctx.guarded(record_run, ctx.shadow, meta, diff, cfg)
    return {"run_id": run_id, "run_dir": str(run_dir), "root": meta["root"], "mode": args.mode,
            "base_warning": sync.warning, "files": len(files), "chunks": chunks, "panel": meta["panel"],
            "guidance": meta["guidance"], "replies": replies}
