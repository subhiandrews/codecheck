"""Evaluate the review pipeline. `--replay` replays the golden cases offline; `--live` reviews the snippets with the
real model (local only, costs tokens); `--calibrate` measures the LLM judge against the golden set's human labels
(local only, costs tokens). The result's `passed` is the verdict. Full `--live` and `--calibrate` runs are also
appended to `docs/eval-history.jsonl` and `--history` rebuilds the report from it (see `evals/history.py`)."""

import sys
import tempfile
from pathlib import Path

from codecheck.errors import CodeCheckError
from codecheck.evals import history, memory_retrieval, memory_semantic
from codecheck.evals.golden import DEFAULT_CASES, run_replay
from codecheck.evals.judge import run_calibration
from codecheck.evals.live import (
    DEFAULT_SNIPPETS,
    load_snippets,
    run_snippet,
    unreachable_reason,
)
from codecheck.evals.memory_effect import run_effect
from codecheck.evals.memory_retrieval import run_retrieval

NAME = "eval"
HELP = "Evaluate the review pipeline against the golden cases or live snippets (developer tooling)"


def add_arguments(p) -> None:
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--replay", action="store_true",
                      help="replay stored reviewer outputs through the deterministic pipeline (no network, no model)")
    mode.add_argument("--live", action="store_true",
                      help="review each snippet with the real panel through `claude -p` (local only; costs tokens)")
    mode.add_argument("--calibrate", action="store_true",
                      help="ask the judge model about every human-labelled golden finding and report its agreement "
                           "with the humans (local only; costs tokens)")
    mode.add_argument("--memory-retrieval", action="store_true",
                      help="score memory retrieval (recall@5, MRR) over tests/memory/retrieval; no model; advisory, never a gate")
    mode.add_argument("--memory-effect", action="store_true",
                      help="review each memory snippet twice, memory off then on, and report what changed (local only; costs tokens; advisory)")
    mode.add_argument("--history", action="store_true",
                      help="rebuild docs/eval-history.md (the score trend) from docs/eval-history.jsonl; no model, no network")
    p.add_argument("--no-record", action="store_true",
                   help="--live, --calibrate: do not append this run to docs/eval-history.jsonl")
    p.add_argument("--cases", type=Path, default=None,
                   help="--replay, --calibrate: directory of golden case files (default: the plugin repository's tests/golden)")
    p.add_argument("--snippets", type=Path, default=None,
                   help="--live: directory of snippet folders (default: the plugin repository's tests/snippets)")
    p.add_argument("--only", default=None, metavar="NAME",
                   help="--live: run just this snippet; --calibrate: just this golden case id")
    p.add_argument("--if-reachable", action="store_true",
                   help="--live, --memory-effect, --memory-retrieval: when no model (or retriever backend) is reachable, report a skip instead of failing")
    p.add_argument("--retriever", default=None, metavar="NAME",
                   help="--memory-retrieval: score just this retriever (default: every registered one)")


def _dir(ctx, given: Path | None, default: Path, what: str) -> Path:
    if given is None:
        return default  # missing in an installed package: then there is nothing to run, which passes
    path = given if given.is_absolute() else ctx.cwd / given
    if not path.is_dir():
        raise CodeCheckError(f"no such {what} directory: {given}")
    return path


def _record(ctx, mode: str, result: dict, only: str | None) -> None:
    """Recording the score must never fail or alter the eval: swallow and warn."""
    try:
        path = history.record(ctx, mode, result, only)
    except Exception as e:  # noqa: BLE001 - recording is best effort by design
        print(f"warning: eval history not recorded: {type(e).__name__}: {e}", file=sys.stderr)
        return
    if path is not None:
        print(f"eval history: appended to {path} and rebuilt {path.with_name(history.REPORT)}; commit both", file=sys.stderr)


def _run_memory(args, ctx) -> dict:
    """The advisory memory evals (decision 0044): nothing here is recorded in the history or gates anything."""
    if args.memory_retrieval:
        if (args.snippets, args.only) != (None, None) or args.no_record:
            raise CodeCheckError("--memory-retrieval takes only --cases, --retriever and --if-reachable")
        memory_semantic.register(ctx)  # only with memory.embed_model set; available only while the embedder answers
        directory = _dir(ctx, args.cases, memory_retrieval.DEFAULT_CASES, "cases")
        if args.cases is None and not any(directory.glob("*.json")):
            # an installed copy has no tests/ directory: running zero cases and passing would hide that
            raise CodeCheckError(f"no retrieval cases found in {directory}; run from a checkout or pass --cases DIR")
        return run_retrieval(directory, args.retriever, args.if_reachable)
    if args.cases is not None or args.retriever is not None or args.no_record:
        raise CodeCheckError("--memory-effect takes only --snippets, --only and --if-reachable")
    return run_effect(ctx, _dir(ctx, args.snippets, DEFAULT_SNIPPETS, "snippets"), args.only, args.if_reachable)


def run(args, ctx) -> dict:
    if args.retriever is not None and not args.memory_retrieval:
        raise CodeCheckError("--retriever applies to --memory-retrieval only")
    if args.memory_retrieval or args.memory_effect:
        return _run_memory(args, ctx)
    if args.history:
        if (args.cases, args.snippets, args.only) != (None, None, None) or args.if_reachable or args.no_record:
            raise CodeCheckError("--history takes no other options")
        return history.regenerate()
    if args.no_record and args.replay:
        raise CodeCheckError("--no-record applies to --live and --calibrate only")
    if args.replay:
        if args.snippets is not None or args.only is not None or args.if_reachable:
            raise CodeCheckError("--snippets and --if-reachable apply to --live only; --only applies to --live and --calibrate")
        # Exit status stays 0 when a case fails: callers (CI, the commit gate) read `passed`.
        return run_replay(_dir(ctx, args.cases, DEFAULT_CASES, "cases"))
    if args.calibrate:
        if args.snippets is not None or args.if_reachable:
            raise CodeCheckError("--snippets and --if-reachable apply to --live only")
        directory = _dir(ctx, args.cases, DEFAULT_CASES, "cases")
        reason = unreachable_reason(ctx)
        if reason is not None:
            raise CodeCheckError(f"model not reachable: {reason}")
        # Exit status stays 0 when the bar is missed: read `passed`.
        result = run_calibration(ctx, directory, args.only)
        if not args.no_record and args.cases is None:  # a custom case set is not comparable with the history
            _record(ctx, "calibrate", result, args.only)
        return result
    if args.cases is not None:
        raise CodeCheckError("--cases applies to --replay and --calibrate only")
    result = run_live(ctx, _dir(ctx, args.snippets, DEFAULT_SNIPPETS, "snippets"), args.only, args.if_reachable)
    if not args.no_record and args.snippets is None:
        _record(ctx, "live", result, args.only)
    return result


def run_live(ctx, directory: Path, only: str | None, if_reachable: bool) -> dict:
    snippets = load_snippets(directory)
    if only is not None:
        names = [s["name"] for s in snippets]
        if only not in names:
            raise CodeCheckError(f"unknown snippet {only!r}; known: {', '.join(names) or 'none'}")
        snippets = [s for s in snippets if s["name"] == only]
    else:  # a full run skips snippets marked "live": false; --only and --memory-effect still run them
        snippets = [s for s in snippets if s.get("live", True)]
    if not snippets:
        return {"passed": True, "cases": []}
    reason = unreachable_reason(ctx)
    if reason is not None:
        if if_reachable:
            return {"passed": True, "skipped": f"model not reachable: {reason}", "cases": []}
        raise CodeCheckError(f"model not reachable: {reason}")
    # One review per snippet, one after another, never retried: each costs real tokens.
    cases = []
    for snippet in snippets:
        with tempfile.TemporaryDirectory(prefix="code-check-eval-", ignore_cleanup_errors=True) as workdir:
            cases.append(run_snippet(ctx, snippet, Path(workdir)))
    # Exit status stays 0 when a case fails, as with --replay: read `passed`.
    return {"passed": all(c["result"]["passed"] for c in cases), "cases": cases}
