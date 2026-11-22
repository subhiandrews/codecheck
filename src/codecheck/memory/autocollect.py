"""Automatic outcome collection: collect what happened to the PRs that are due, then fold the evidence into memory.

Collection is evidence gathering, not reading memory, so it also runs in blind hand-off runs: nothing collected here is
shown to a reviewer of that run. It reuses `collect_outcomes` (the same function `shadow collect` calls) and
`consolidate_store`, and it never raises: a provider, store or state failure becomes an entry in the result.
"""

import threading
import time
from collections.abc import Callable

from codecheck.config import expand_path
from codecheck.memory.consolidate import Params
from codecheck.memory.db import MemoryDB
from codecheck.memory.pipeline import consolidate_store
from codecheck.memory.schedule import MAX_PRS, due, mark_collected, run_budgeted
from codecheck.shadow.collect import collect_outcomes


def collect_due(ctx, *, everything: bool = False, dry_run: bool = False, now: Callable[[], float] = time.time,
                mono: Callable[[], float] = time.monotonic, max_prs: int = MAX_PRS) -> dict:
    result: dict = {"due": [], "collected": [], "errors": [], "consolidated": False}
    try:
        if ctx.shadow is None:
            return result
        stamp = now()
        cands = due(ctx.state, ctx.shadow, stamp, everything=everything)
        result["due"] = [c.pr for c in cands]
        if dry_run or not cands:
            return result
        me = ctx.provider.whoami()

        def work(cand) -> None:
            try:
                ref = ctx.parse_ref(cand.pr)
                try:
                    pr_state = ctx.provider.get_pr(ref).state
                except Exception:  # noqa: BLE001 - only needed for the closing time
                    pr_state = None
                collect_outcomes(ctx.provider, ctx.shadow, ref, me)
                mark_collected(ctx.state, cand, stamp, pr_state=pr_state)
                result["collected"].append(cand.pr)
            except Exception as e:  # noqa: BLE001 - one PR failing must not stop the others
                result["errors"].append({"pr": cand.pr, "error": f"{type(e).__name__}: {e}"})

        run_budgeted(cands, work, mono, max_prs=max_prs)
        if result["collected"]:
            m = ctx.config.memory
            params = Params(half_days=float(m.half_life_days), expire_days=float(m.expire_days))
            consolidate_store(ctx.shadow, MemoryDB(expand_path(m.store), now=lambda: stamp), params, stamp)
            result["consolidated"] = True
            if m.url and m.share_repos:
                result["pushed"] = _push_shared(ctx)
    except Exception as e:  # noqa: BLE001 - never raises
        result["errors"].append({"error": f"{type(e).__name__}: {e}"})
    return result


def _push_shared(ctx) -> dict:
    """Send what consolidation produced to the shared server, through the sharing levels. Never raises: a down server
    is retried by the next collection (the outbox in `sync.json` remembers what it accepted). Only called with
    `memory.url` and a non-empty `memory.share_repos`, so the default configuration sends nothing."""
    try:
        from codecheck.memory import sync

        budget = max(float(ctx.config.memory.timeout_seconds), 0.5)
        box: dict = {}
        cancel = threading.Event()

        def work() -> None:
            try:
                box["out"] = sync.sync(ctx, do_pull=False, http_timeout=budget, cancel=cancel)
            except BaseException as e:  # noqa: BLE001 - nothing in the worker may escape
                box["out"] = {"ok": False, "error": type(e).__name__}

        t = threading.Thread(target=work, name="code-check-memory-push", daemon=True)
        t.start()
        t.join(budget)  # a hard wall clock: cleanup is never delayed longer than memory.timeout_seconds by the server
        if t.is_alive():
            cancel.set()  # the abandoned push may finish its request, but it can no longer write sync.json
            return {"ok": False, "error": "timeout"}
        out = box.get("out") or {}
        return {"ok": bool(out.get("ok")), **({"error": out["error"]} if "error" in out else {})}
    except Exception as e:  # noqa: BLE001 - collection must never fail because the server did
        return {"ok": False, "error": type(e).__name__}
