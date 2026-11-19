"""Export hand-offs to an OpenTelemetry collector (OTLP/HTTP JSON). Off unless `telemetry.enabled` is true."""

from codecheck.errors import CodeCheckError
from codecheck.runids import check_run_id
from codecheck.telemetry.push import push_runs, pushable_runs

NAME = "telemetry"
HELP = "Export hand-offs (traces, tool failures, findings, human comments) to an OTLP collector"


def add_arguments(p) -> None:
    sub = p.add_subparsers(dest="telemetry_cmd", required=True)
    push = sub.add_parser("push", help="send hand-offs that changed since they were last sent")
    g = push.add_mutually_exclusive_group(required=True)
    g.add_argument("--run", help="one run id")
    g.add_argument("--all", action="store_true", help="every run that has a hand-off (retries failed sends)")
    push.add_argument("--dry-run", action="store_true", help="print the OTLP payload that would be sent; contacts nothing")
    push.add_argument("--force", action="store_true", help="send even when the payload is unchanged since the last send")


def run(args, ctx) -> dict:
    if ctx.shadow is None:
        raise CodeCheckError("shadow mode is disabled (shadow.enabled = false)")
    run_ids = [check_run_id(args.run)] if args.run else pushable_runs(ctx.shadow)
    return push_runs(ctx, run_ids, dry_run=args.dry_run, force=args.force)
