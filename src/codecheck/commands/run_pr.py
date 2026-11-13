import json
import time

from codecheck.commands._common import safe_repo
from codecheck.commands.workspace import managed_clone
from codecheck.config import expand_path
from codecheck.core.state import (
    MAX_LOOP_FAILURES,
    WorkItem,
    mark_processed,
    record_failure,
)
from codecheck.errors import CodeCheckError
from codecheck.runids import new_run_id

NAME = "run-pr"
HELP = "Run one headless Claude process for one PR (review or babysit)"


def add_arguments(p) -> None:
    p.add_argument("--mode", choices=("review", "babysit"), required=True)
    p.add_argument("--pr", required=True)
    p.add_argument("--fingerprint", default=None,
                   help="loop bookkeeping: mark this work item processed on success (or after repeated failures)")
    p.add_argument("--security", action="store_true", help="review mode only: also run the security reviewer")
    p.add_argument("--dry-run", action="store_true")


def parse_result(stdout: str, returncode: int, stderr: str) -> dict:
    """Normalise `claude -p --output-format json` output (key names confirmed by the Task 1 spike)."""
    try:
        data = json.loads(stdout)
        if not isinstance(data, dict):
            raise TypeError("not an object")
    except (json.JSONDecodeError, TypeError, ValueError):
        text = stdout.strip() or stderr.strip()
        if returncode == 124:
            return {"ok": False, "summary": f"timed out: {stderr.strip()}", "cost": None, "usage": None}
        why = "not json" if stdout.strip() else f"exit {returncode}"
        return {"ok": False, "summary": f"{why}: {text[-400:]}", "cost": None, "usage": None}
    result = str(data.get("result", ""))[-500:]
    ok = returncode == 0 and not data.get("is_error", False)
    return {"ok": ok, "summary": result if ok else f"error: {result or stderr.strip()[-400:]}",
            "cost": data.get("total_cost_usd"), "usage": data.get("usage")}


def run_one(ctx, mode: str, ref, fingerprint: str | None = None, dry_run: bool = False,
            security: bool = False) -> dict:
    if security and mode != "review":
        raise CodeCheckError("--security applies to review mode only")
    cfg = ctx.config.run_pr
    if ref.repo.startswith("-"):
        raise CodeCheckError(f"unsafe repository name {ref.repo!r}")
    template = cfg.review_prompt if mode == "review" else cfg.babysit_prompt
    tools = cfg.review_allowed_tools if mode == "review" else cfg.babysit_allowed_tools
    prompt = template.format(ref=ref)
    if security:
        prompt += " Pass the security argument."
    # argv values must never be read as options by claude.
    if prompt.lstrip().startswith("-") or any(t.lstrip().startswith("-") or not t.strip() for t in tools):
        raise CodeCheckError("run_pr prompt and allowed tools must not start with '-'")
    # The prompt comes first: --allowedTools and --add-dir are variadic and would swallow a later positional.
    argv = [cfg.claude_bin, "-p", prompt, "--output-format", "json",
            "--permission-mode", cfg.permission_mode, "--permission-prompts", "none"]
    if cfg.add_state_dir:
        # Reviews and fixes happen in worktrees and run directories under state_dir, outside the run's cwd.
        # An absolute path never starts with '-'. (`--add-dir` is not covered by the Task 1 spike.)
        argv += ["--add-dir", str(expand_path(ctx.config.state_dir).resolve())]
    argv += ["--allowedTools", *tools]
    if dry_run:
        return {"pr": str(ref), "argv": argv}
    started = time.time()
    try:
        # Babysit edits code: never let an unattended run work in the user's own checkout, always in the
        # managed clone (whose worktrees live under state_dir). A review only reads, so the user's clone is fine.
        if mode == "review" and safe_repo(ctx) == ref.repo:
            cwd = ctx.git.cwd
        else:
            cwd = managed_clone(ctx, ref.repo).cwd
        # input="" closes stdin: claude -p otherwise waits for it.
        res = ctx.runner.run(argv, cwd=cwd, timeout=cfg.timeout_seconds, input="")
        parsed = parse_result(res.stdout, res.returncode, res.stderr)
    except Exception as e:  # noqa: BLE001 - report, and still do the bookkeeping below
        parsed = {"ok": False, "summary": f"error: {type(e).__name__}: {e}"[:500], "cost": None, "usage": None}
    duration = round(time.time() - started, 1)
    extra = {}
    if fingerprint:
        extra = loop_bookkeeping(ctx, mode, ref, fingerprint, parsed["ok"])
    ctx.record(new_run_id(), {"type": "headless_run", "pr": str(ref), "mode": mode, "ok": parsed["ok"],
                              "cost_usd": parsed["cost"], "usage": parsed["usage"], "duration_s": duration})
    return {"pr": str(ref), "ok": parsed["ok"], "summary": parsed["summary"], "cost_usd": parsed["cost"],
            "duration_s": duration, **extra}


def loop_bookkeeping(ctx, mode: str, ref, fingerprint: str, ok: bool) -> dict:
    """Success marks the loop item processed. A failure is retried on later iterations until it has failed
    MAX_LOOP_FAILURES times for the same fingerprint. Never touches `review:<ref>`, which only `post` sets."""
    item = WorkItem(ref, fingerprint, "")
    if ok:
        mark_processed(ctx.state, mode, item)
        return {}
    count, gave_up = record_failure(ctx.state, mode, item)
    return {"failures": count, "gave_up": gave_up,
            "retry": "no: waiting for a change on the PR" if gave_up
            else f"yes: attempt {count} of {MAX_LOOP_FAILURES} failed"}


def run(args, ctx) -> dict:
    return run_one(ctx, args.mode, ctx.resolve_ref(args.pr), args.fingerprint, args.dry_run, args.security)
