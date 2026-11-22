"""`memory serve | token | sync | status`: the subcommands for the shared memory server, registered by commands/memory.py.

`serve` is the one command that does not print a single JSON document at the end: it prints one JSON line when it starts
listening (so a supervisor can read the address) and then runs until it is stopped, exiting 0 without printing anything more.
"""

import json
import signal
import threading

from codecheck.config import MEMORY_LEVELS, expand_path
from codecheck.errors import CodeCheckError
from codecheck.memory import sync
from codecheck.memory.server import ServerDB, Service, make_server

COMMANDS = ("serve", "token", "sync", "status")


def default_db(ctx) -> str:
    return str(sync.default_solo_db(ctx))


def add_arguments(sub) -> None:
    s = sub.add_parser("serve", help="run the memory server (long-running: prints one JSON line when listening, then runs)")
    s.add_argument("--host", default="127.0.0.1", help="address to bind; anything but loopback needs TLS or --behind-proxy")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--db", default="", help="directory of the server's store (default: next to memory.store)")
    s.add_argument("--max-level", choices=MEMORY_LEVELS, default="lessons", help="most detail the server accepts (default lessons)")
    s.add_argument("--tls-cert", default="")
    s.add_argument("--tls-key", default="")
    s.add_argument("--behind-proxy", action="store_true", help="a reverse proxy in front terminates TLS")
    s.add_argument("--allow-host", action="append", default=[], help="a Host header value to accept (repeatable)")
    s.add_argument("--max-concurrent", type=int, default=32)

    t = sub.add_parser("token", help="manage the server's tokens (run on the server's machine)").add_subparsers(dest="token_cmd", required=True)
    a = t.add_parser("add", help="create a token; it is printed once and only its hash is stored")
    a.add_argument("name", help="the contributor id; their evidence is removable by it")
    a.add_argument("--admin", action="store_true", help="may also delete a contributor's evidence")
    a.add_argument("--maintainer", action="store_true",
                   help="may assert the maintainer role on evidence (for people with write access); without it a claimed "
                        "maintainer is stored as a contributor")
    a.add_argument("--db", default="")
    r = t.add_parser("revoke", help="stop a token working")
    r.add_argument("name")
    r.add_argument("--purge", action="store_true", help="also delete the evidence and episodes this contributor sent")
    r.add_argument("--db", default="")
    ls = t.add_parser("list", help="token names and roles (never a token)")
    ls.add_argument("--db", default="")

    y = sub.add_parser("sync", help="push shared lessons to the server and pull a repository's evidence back")
    y.add_argument("--push-only", action="store_true")
    y.add_argument("--pull-only", action="store_true")
    y.add_argument("--repo", action="append", default=[], help="also pull this repository (owner/name)")
    y.add_argument("--db", default="", help="solo mode only: the local server directory to sync with")

    st = sub.add_parser("status", help="effective sharing level per repository, server meta and the last sync")
    st.add_argument("--offline", action="store_true", help="make no network or provider calls")
    st.add_argument("--db", default="", help="solo mode only: the local server directory")


def _db(ctx, args) -> ServerDB:
    return ServerDB(expand_path(args.db or default_db(ctx))).init()


def run(args, ctx) -> dict:
    cmd = args.memory_cmd
    if cmd == "serve":
        return serve(args, ctx)
    if cmd == "token":
        db = _db(ctx, args)
        action = args.token_cmd
        if action == "add":
            ceiling = "maintainer" if args.maintainer else "contributor"
            token = db.add_token(args.name, "admin" if args.admin else "contributor", ceiling)
            return {"name": args.name, "role": "admin" if args.admin else "contributor", "ceiling": ceiling, "token": token,
                    "note": "shown once; only its hash is stored. Put it in a 0600 file named by memory.token_file."}
        if action == "revoke":
            done = db.revoke_token(args.name)
            if not done:
                raise CodeCheckError(f"no live token named {args.name!r}")
            purged = db.purge_contributor(args.name, Service(db).params, db.now()) if args.purge else None
            return {"revoked": args.name, "purged": purged}
        return {"tokens": db.list_tokens()}
    if cmd == "sync":
        if args.push_only and args.pull_only:
            raise CodeCheckError("--push-only and --pull-only exclude each other")
        return sync.sync(ctx, do_push=not args.pull_only, do_pull=not args.push_only, solo_db=args.db, extra_repos=args.repo)
    return sync.status(ctx, offline=args.offline, solo_db=args.db)


def serve(args, ctx) -> dict:
    db = _db(ctx, args)
    service = Service(db, args.max_level, sync.params_of(ctx))
    server = make_server(service, args.host, args.port, tls_cert=args.tls_cert, tls_key=args.tls_key,
                         behind_proxy=args.behind_proxy, allow_hosts=args.allow_host, max_concurrent=args.max_concurrent)
    host, port = server.server_address[:2]
    print(json.dumps({"serving": True, "address": f"{host}:{port}", "tls": bool(args.tls_cert), "behind_proxy": args.behind_proxy,
                      "db": str(db.file), "max_level": args.max_level}), flush=True)
    if threading.current_thread() is threading.main_thread():
        def stop(_signum, _frame):
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    raise SystemExit(0)
