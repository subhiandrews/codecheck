import contextlib
import sys
from pathlib import Path

from codecheck import __version__
from codecheck.config import expand_path
from codecheck.core.feedback.draft import read_draft, render, write_draft
from codecheck.core.feedback.file import Filer
from codecheck.core.feedback.manifest import Manifest
from codecheck.core.feedback.signals import make_signal
from codecheck.core.feedback.store import FeedbackStore
from codecheck.errors import CodeCheckError

NAME = "feedback"
HELP = "Consent-gated feedback: record a signal, list drafts to ask about, and file one only on the user's answer"

KINDS = {
    "attempt-exhausted": "CI fix attempts ran out",
    "rerun-refused": "A check rerun was refused",
    "findings-missing": "Reviewer findings files were missing or stray",
    "push-refused": "A push was refused",
    "loop-failed": "An unattended run failed or timed out",
    "thread-resolved-unfixed": "A thread was resolved with no fix",
    "finding-disputed": "A finding was disputed",
    "agent-off-track": "An agent went off track",
    "user-reported": "Reported by the user",
}
UPSTREAM = "subhiandrews/codecheck"
SNOOZE_DAYS = 7
_OFF = {"off", "0", "false", "no"}


def add_arguments(p) -> None:
    sub = p.add_subparsers(dest="feedback_cmd", required=True)
    s = sub.add_parser("signal", help="record that something went wrong and write the scratch draft; never prompts")
    s.add_argument("--kind", required=True)
    s.add_argument("--source", required=True, help="the skill or command that noticed it")
    s.add_argument("--id", action="append", default=[], metavar="KEY=VALUE", help="a short label such as step=push; never a PR, repository or check name, never free text")
    s.add_argument("--note", help="what the user wrote about it (user-reported only); `-` reads it from stdin, which keeps it away from the shell")
    s.add_argument("--judged", action="store_true", help="the model noticed it, no detector in code did")
    d = sub.add_parser("pending", help="whether to ask now, and the drafts to show")
    d.add_argument("--interactive", action="store_true", help="a person is in the session and can answer")
    d.add_argument("--user-initiated", action="store_true", help="the user asked to file this; skips the daily limit, snooze and never, but not the consent")
    d.add_argument("--only", help="with --user-initiated: the one draft (fingerprint) to show")
    a = sub.add_parser("answer", help="act on the user's answer for one draft")
    a.add_argument("--id", required=True, help="the draft's fingerprint")
    a.add_argument("--choice", required=True, choices=["post", "decline", "snooze", "never"])
    a.add_argument("--user-initiated", action="store_true", help="the user asked to file this; does not use up the day")
    sub.metavar = "{signal,pending,answer}"


def manifest_for(ctx) -> Manifest:
    return Manifest(product="code-check", version=__version__, target_repo=ctx.config.feedback.target_repo or UPSTREAM,
                    kinds=KINDS)


def unattended(ctx) -> bool:
    return any(str(ctx.env.get(k, "")).strip() == "1" for k in ("CODE_CHECK_UNATTENDED", "CODE_CHECK_HEADLESS"))


def disabled(ctx) -> bool:
    return not ctx.config.feedback.enabled or str(ctx.env.get("CODE_CHECK_FEEDBACK", "")).strip().lower() in _OFF


def _dir(ctx) -> Path:
    return expand_path(ctx.config.state_dir) / "feedback"


def _draft(ctx, m: Manifest, entry: dict) -> dict:
    path = _dir(ctx) / f"{entry['fingerprint']}.md"
    if not path.exists():
        title, body = render(m, entry["kind"], entry["source"], entry["fingerprint"], entry["detail"],
                             judged=entry["judged"])
        write_draft(_dir(ctx), entry["fingerprint"], title, body)
    title, body = read_draft(path)
    return {"fingerprint": entry["fingerprint"], "kind": entry["kind"], "count": entry["count"], "judged": entry["judged"],
            "title": title, "body": body, "path": str(path)}


def record(ctx, kind: str, source: str, ids: dict, *, judged: bool = False) -> dict:
    """Record one signal and (re)write its scratch draft. Never prompts."""
    if disabled(ctx):
        return {"recorded": False, "reason": "disabled"}
    m = manifest_for(ctx)
    m.title_for(kind)
    try:
        signal = make_signal(kind, source, ids, judged=judged)
    except (TypeError, ValueError) as e:
        raise CodeCheckError(str(e)) from e
    entry = FeedbackStore(ctx.state).add(signal)
    if entry is None:
        return {"recorded": False, "reason": "never"}
    title, body = render(m, entry["kind"], entry["source"], entry["fingerprint"], entry["detail"],
                         judged=entry["judged"])
    path = _dir(ctx) / f"{entry['fingerprint']}.md"
    if not path.exists():  # a draft the user is editing is never rewritten
        path = write_draft(_dir(ctx), entry["fingerprint"], title, body)
    return {"recorded": True, "fingerprint": entry["fingerprint"], "count": entry["count"], "draft": str(path)}


def _signal(args, ctx) -> dict:
    ids = {}
    for item in args.id:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise CodeCheckError(f"--id must be KEY=VALUE, got {item!r}")
        ids[key] = value
    if args.note is not None:
        if args.kind != "user-reported":
            raise CodeCheckError("--note is only for the user-reported kind")
        if unattended(ctx):  # a run steered by PR text must not queue a draft that reads as the user's own report
            raise CodeCheckError("a user-reported note cannot be recorded in an unattended run")
        ids["note"] = sys.stdin.read().strip() if args.note == "-" else args.note
    return record(ctx, args.kind, args.source, ids, judged=args.judged)


def _pending(args, ctx) -> dict:
    store, m = FeedbackStore(ctx.state), manifest_for(ctx)
    none = {"ask": False, "drafts": []}
    if disabled(ctx):
        return {**none, "reason": "disabled"}
    if unattended(ctx):
        return {**none, "reason": "unattended"}
    if not args.interactive:
        return {**none, "reason": "not-interactive"}
    if args.user_initiated:
        entry = store.get(args.only or "")
        if entry is None:
            raise CodeCheckError("no such draft; record it first with `feedback signal`")
        return {"ask": True, "reason": "user-initiated", "drafts": [_draft(ctx, m, entry)]}
    ask, reason = store.decide(ctx.config.feedback.min_interval_hours)
    if ask:  # showing the draft is the ask: a dismissed prompt must not come back in the same day
        store.mark_asked()
    return {"ask": ask, "reason": reason, "drafts": [_draft(ctx, m, e) for e in store.pending()] if ask else []}


def _answer(args, ctx) -> dict:
    store, m = FeedbackStore(ctx.state), manifest_for(ctx)
    if args.choice == "post" and (disabled(ctx) or unattended(ctx)):  # the same gate `pending` applies, enforced where the issue is filed
        raise CodeCheckError("feedback is turned off or this run is unattended; nothing was filed")
    entry = store.get(args.id)
    if entry is None and args.choice in ("post", "decline"):  # snooze and never need no draft: an opt-out is never lost
        raise CodeCheckError(f"no pending draft {args.id!r}")
    path = _dir(ctx) / f"{args.id}.md"
    out: dict = {"choice": args.choice}
    if args.choice == "post":
        _draft(ctx, m, entry)  # recreates the file if it was deleted
        title, body = read_draft(path)
        res = Filer(ctx.runner, m, cwd=ctx.cwd).file(title, body)
        out |= {"outcome": res.outcome, "url": res.url, "path": str(path)}
        if res.outcome != "fallback":  # a fallback has not filed anything: keep the draft for the user to send by hand
            store.remove(args.id)
            path.unlink(missing_ok=True)
    elif args.choice == "decline":
        store.remove(args.id)
        path.unlink(missing_ok=True)
    elif args.choice == "snooze":
        store.snooze(SNOOZE_DAYS)
    else:
        store.set_never()
        for f in _dir(ctx).glob("*.md"):
            with contextlib.suppress(OSError):
                f.unlink()
    if not args.user_initiated:
        store.mark_asked()
    return out


def run(args, ctx) -> dict:
    return {"signal": _signal, "pending": _pending, "answer": _answer}[args.feedback_cmd](args, ctx)
