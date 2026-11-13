import json
import sys

from codecheck.errors import CodeCheckError
from codecheck.models import PRRef
from codecheck.shadow.collect import collect_outcomes
from codecheck.shadow.export import export_golden

NAME = "shadow"
HELP = "Shadow-mode data: collect human feedback, export a golden set, record verdicts"
VERDICTS = {"valid", "partial", "incorrect", "unclear", "preference", "out_of_scope"}
_VERDICT_FIELDS = ("thread_id", "verdict", "reasoning", "action", "commit")


def add_arguments(p) -> None:
    sub = p.add_subparsers(dest="shadow_cmd", required=True)
    c = sub.add_parser("collect", help="match threads to posted findings and record human feedback")
    g = c.add_mutually_exclusive_group(required=True)
    g.add_argument("--pr")
    g.add_argument("--all", action="store_true")
    e = sub.add_parser("export-golden", help="write the auto-labelled golden set as JSONL")
    e.add_argument("--out", required=True)
    v = sub.add_parser("record-verdict", help="record address-comments verdicts (JSON list on stdin)")
    v.add_argument("--run", required=True)


def _prs_in_store(shadow) -> list[str]:
    seen = []
    for rid in shadow.run_ids():
        for rec in shadow.read(rid):
            if rec.get("type") == "run" and rec.get("pr") and rec["pr"] not in seen:
                seen.append(rec["pr"])
    return seen


def _record_verdicts(args, ctx) -> dict:
    try:
        items = json.loads(sys.stdin.read())
    except json.JSONDecodeError as e:
        raise CodeCheckError(f"stdin is not valid JSON: {e}") from e
    if not isinstance(items, list):
        raise CodeCheckError("stdin must be a JSON list of verdict objects")
    for item in items:
        if not isinstance(item, dict) or item.get("verdict") not in VERDICTS:
            raise CodeCheckError(f"each item needs a verdict in {sorted(VERDICTS)}")
        if not isinstance(item.get("thread_id"), str):
            raise CodeCheckError("each item needs a string thread_id")
    if not ctx.shadow.read(args.run):  # also validates the run id
        raise CodeCheckError(f"unknown run {args.run}: nothing recorded for it")
    for item in items:
        ctx.shadow.append(args.run, {"type": "verdict", **{k: item[k] for k in _VERDICT_FIELDS if k in item}})
    return {"recorded": len(items)}


def run(args, ctx) -> dict:
    if ctx.shadow is None:
        raise CodeCheckError("shadow mode is disabled (shadow.enabled = false)")
    if args.shadow_cmd == "export-golden":
        return {"exported": export_golden(ctx.shadow, args.out), "out": args.out}
    if args.shadow_cmd == "record-verdict":
        return _record_verdicts(args, ctx)
    me = ctx.provider.whoami()
    if args.pr:
        return collect_outcomes(ctx.provider, ctx.shadow, ctx.resolve_ref(args.pr), me)
    results = []
    for pr in _prs_in_store(ctx.shadow):
        try:
            results.append({"pr": pr, **collect_outcomes(ctx.provider, ctx.shadow, PRRef.parse(pr), me)})
        except (CodeCheckError, ValueError) as e:
            results.append({"pr": pr, "error": str(e)})
    return {"results": results}
