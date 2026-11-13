import json
import sys
from pathlib import Path

from codecheck.errors import CodeCheckError
from codecheck.evals.golden import DEFAULT_CASES
from codecheck.models import PRRef
from codecheck.runids import check_run_id
from codecheck.shadow.collect import collect_outcomes
from codecheck.shadow.export import export_golden
from codecheck.shadow.promote import draft_case, finalize_case
from codecheck.shadow.scrub import scrub
from codecheck.shadow.store import write_private

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
    # Developer tooling for hand-off runs, hidden from help like `handoff` (a `help=` would print "==SUPPRESS==").
    pr = sub.add_parser("promote")
    mode = pr.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run", help="draft a golden case from this hand-off run")
    mode.add_argument("--finalize", help="check a confirmed draft and write it into the golden set")
    pr.add_argument("--out", help="where to write the draft (with --run)")
    pr.add_argument("--allow-private", action="store_true", help="promote a case from a private or unknown repository")
    pr.add_argument("--dest", default=None, help="golden case directory (default: the plugin repository's tests/golden)")
    sub.metavar = "{collect,export-golden,record-verdict}"


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


def _path(ctx, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ctx.cwd / path


def _promote(args, ctx) -> dict:
    if args.finalize:
        if args.dest is not None:
            dest = _path(ctx, args.dest)
        elif DEFAULT_CASES.is_dir():
            dest = DEFAULT_CASES
        else:  # an installed package has no tests/golden; never invent one inside site-packages
            raise CodeCheckError("no tests/golden directory next to this install; pass --dest")
        case = finalize_case(_path(ctx, args.finalize), dest, allow_private=args.allow_private)
        return {"case": str(case), "case_id": json.loads(case.read_text(encoding="utf-8"))["case_id"]}
    if not args.out:
        raise CodeCheckError("promote --run needs --out <draft path>")
    if ctx.shadow is None:
        raise CodeCheckError("shadow mode is disabled (shadow.enabled = false)")
    case = draft_case(ctx.shadow, check_run_id(args.run))
    out = _path(ctx, args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_private(out, json.dumps(scrub(case), indent=2, ensure_ascii=False) + "\n")  # private code: 0600 until confirmed
    return {"draft": str(out), "case_id": case["case_id"], "label_status": case["label_status"],
            "labels": case["labels"], "expected_decision": case["expected_decision"],
            "next": "check each label (expected or not_expected), set label_status to \"confirmed\", "
                    "then run: code-check shadow promote --finalize <draft>"}


def run(args, ctx) -> dict:
    if args.shadow_cmd == "promote":
        return _promote(args, ctx)
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
