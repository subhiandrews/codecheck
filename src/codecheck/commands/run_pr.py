import json
import time

from codecheck.commands._common import safe_repo
from codecheck.commands.workspace import managed_clone
from codecheck.core.state import WorkItem, mark_processed
from codecheck.errors import CodeCheckError
from codecheck.runids import new_run_id

NAME = "run-pr"
HELP = "Run one headless Claude process for one PR (review or babysit)"


def add_arguments(p) -> None:
    p.add_argument("--mode", choices=("review", "babysit"), required=True)
    p.add_argument("--pr", required=True)
    p.add_argument("--fingerprint", default=None, help="mark this work item processed when done")
    p.add_argument("--dry-run", action="store_true")


def parse_result(stdout: str, returncode: int, stderr: str) -> dict:
    """Normalise `claude -p --output-format json` output (key names confirmed by the Task 1 spike)."""
    try:
        data = json.loads(stdout)
        if not isinstance(data, dict):
            raise ValueError("not an object")
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


def run_one(ctx, mode: str, ref, fingerprint: str | None = None, dry_run: bool = False) -> dict:
    cfg = ctx.config.run_pr
    if ref.repo.startswith("-"):
        raise CodeCheckError(f"unsafe repository name {ref.repo!r}")
    template = cfg.review_prompt if mode == "review" else cfg.babysit_prompt
    # The prompt comes first: --allowedTools is variadic and would swallow a later positional.
    argv = [cfg.claude_bin, "-p", template.format(ref=ref), "--output-format", "json",
            "--permission-mode", cfg.permission_mode, "--permission-prompts", "none",
            "--allowedTools", *cfg.allowed_tools]
    if dry_run:
        return {"pr": str(ref), "argv": argv}
    started = time.time()
    try:
        cwd = ctx.git.cwd if safe_repo(ctx) == ref.repo else managed_clone(ctx, ref.repo).cwd
        # input="" closes stdin: claude -p otherwise waits for it.
        res = ctx.runner.run(argv, cwd=cwd, timeout=cfg.timeout_seconds, input="")
        parsed = parse_result(res.stdout, res.returncode, res.stderr)
    except Exception as e:  # noqa: BLE001 - report, and still mark processed below
        parsed = {"ok": False, "summary": f"error: {type(e).__name__}: {e}"[:500], "cost": None, "usage": None}
    duration = round(time.time() - started, 1)
    if fingerprint:
        mark_processed(ctx.state, mode, WorkItem(ref, fingerprint, ""))
    ctx.record(new_run_id(), {"type": "headless_run", "pr": str(ref), "mode": mode, "ok": parsed["ok"],
                              "cost_usd": parsed["cost"], "usage": parsed["usage"], "duration_s": duration})
    return {"pr": str(ref), "ok": parsed["ok"], "summary": parsed["summary"], "cost_usd": parsed["cost"],
            "duration_s": duration}


def run(args, ctx) -> dict:
    return run_one(ctx, args.mode, ctx.resolve_ref(args.pr), args.fingerprint, args.dry_run)
