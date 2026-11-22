"""Build `memory.md` for a review without ever breaking it.

`safe_build` is the only entry point the review commands use. It catches everything, enforces `memory.timeout_seconds`
and returns a small dict that says what happened (`read`, `reason`, and the lesson ids when a file was written). A
corrupt store, a missing git ref, a bug or a slow disk costs the review nothing but its memory.
"""

import threading
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

from codecheck.config import expand_path
from codecheck.memory.db import MemoryDB
from codecheck.memory.render import render_memory
from codecheck.memory.retrieve import finding_lessons, merge_ranked, miss_lessons

FILE_NAME = "memory.md"

REVIEWERS = "reviewers"
AGGREGATOR = "aggregator"


def _gate(cfg, stage: str, blind: bool) -> str | None:
    """Why nothing should be read, or None when memory applies to this stage."""
    if not cfg.enabled:
        return "disabled"
    if not cfg.read:
        return "read off"
    if blind:
        return "blind"  # hand-off runs stay independent of everything a human said (decision 0008)
    if stage == REVIEWERS and not cfg.reviewers:
        return "reviewers off"
    return None


def _build(ctx, *, stage: str, repo: str, base_ref: str, paths: Iterable[str],
           findings: Iterable[tuple[str, object]], queries: Sequence = ()) -> tuple[str, list[str]] | str:
    """The text and lesson ids, or a reason nothing is written. Runs in a worker thread under the time budget."""
    cfg = ctx.config.memory
    db = MemoryDB(expand_path(cfg.store))
    if not db.exists():
        return "no store"
    files = ctx.git.list_files(base_ref)
    if stage == REVIEWERS:
        lessons = miss_lessons(db, repo, paths, files, cfg.max_lessons)
    else:
        lessons = finding_lessons(db, repo, findings, files, cfg.max_lessons)
    if cfg.embed_model and queries:  # optional second stage: lessons found by similarity, after the exact-key hits
        from codecheck.memory import semantic

        kind = "miss" if stage == REVIEWERS else "finding"
        lessons = merge_ranked(db, repo, kind, lessons, semantic.ranked_for(ctx, db, repo, list(queries), kind), files,
                               cfg.max_lessons)
    if not lessons:
        return "no lessons"
    evidence = {le.id: db.evidence(lesson_id=le.id) for le in lessons}
    text = render_memory(lessons, evidence, for_aggregator=stage == AGGREGATOR, max_lessons=cfg.max_lessons,
                         max_chars=cfg.max_chars)
    return text, [le.id for le in lessons]


def _pull_replica(ctx, stage: str, budget: float) -> dict | None:
    """Refresh the local replica from the shared server before the reviewers read it. Never raises, never waits past
    half of `memory.timeout_seconds` (the build keeps the rest), and a failure just leaves the replica as it is.

    Only for the reviewers stage (the aggregator runs after `context` pulled), only with a configured `memory.url`.
    Blind runs never get here: `_gate` returns first."""
    try:
        if stage != REVIEWERS or not ctx.config.memory.url:
            return None
        from codecheck.memory import sync
        box: dict = {}

        def work() -> None:
            try:
                box["out"] = sync.pull_if_stale(ctx)
            except BaseException as e:  # noqa: BLE001 - nothing in the worker may escape
                box["out"] = {"pulled": False, "error": type(e).__name__}

        t = threading.Thread(target=work, name="code-check-memory-pull", daemon=True)
        t.start()
        t.join(budget / 2)
        if t.is_alive():
            return {"pulled": False, "reason": "timeout"}
        out = box.get("out") or {}
        return {k: out[k] for k in ("pulled", "reason", "error") if k in out}
    except Exception as e:  # noqa: BLE001
        return {"pulled": False, "error": type(e).__name__}


def safe_build(ctx, run_dir: Path, *, stage: str, repo: str | None, base_ref: str | None, blind: bool,
               paths: Iterable[str] = (), findings: Iterable[tuple[str, object]] = (), queries: Sequence = ()) -> dict:
    """Write `run_dir/memory.md` when there is something to say. Never raises.

    Returns `{"read": False, "reason": ...}` or `{"read": True, "reason": "ok", "stage", "lessons", "ids", "chars"}`;
    `file` is set exactly when memory.md was written."""
    try:
        cfg = ctx.config.memory
        why = _gate(cfg, stage, blind)
        if why:
            return {"read": False, "reason": why}
        if not repo or not base_ref:
            return {"read": False, "reason": "no repository or base"}
        started = time.monotonic()
        budget = max(float(cfg.timeout_seconds), 0.0)
        pull = _pull_replica(ctx, stage, budget)
        box: dict = {}

        def done(result: dict) -> dict:
            if pull:
                result["pull"] = pull  # only present when memory.url is set
            return result

        def work() -> None:
            try:
                box["out"] = _build(ctx, stage=stage, repo=repo, base_ref=base_ref, paths=list(paths), findings=list(findings),
                                  queries=list(queries))
            except BaseException as e:  # noqa: BLE001 - nothing in the worker may escape
                box["error"] = f"{type(e).__name__}: {e}"[:200]

        worker = threading.Thread(target=work, name="code-check-memory", daemon=True)
        worker.start()
        worker.join(max(budget - (time.monotonic() - started), 0.0))  # the pull and the build share one budget
        if worker.is_alive():
            return done({"read": False, "reason": "timeout"})
        if "error" in box:
            return done({"read": False, "reason": "error", "error": box["error"]})
        out = box.get("out")
        if isinstance(out, str):
            return done({"read": False, "reason": out})
        text, ids = out
        (run_dir / FILE_NAME).write_text(text, encoding="utf-8")
        return done({"read": True, "reason": "ok", "stage": stage, "lessons": len(ids), "ids": ids, "chars": len(text),
                     "file": FILE_NAME})
    except BaseException as e:
        if isinstance(e, KeyboardInterrupt):
            raise
        return {"read": False, "reason": "error", "error": f"{type(e).__name__}: {e}"[:200]}
