"""`memory list` and `memory forget`: see what has been learned, and remove a lesson before it can reach a review."""

import re

from codecheck.config import expand_path
from codecheck.errors import CodeCheckError
from codecheck.memory.db import MemoryDB
from codecheck.memory.model import STATUSES, Lesson

COMMANDS = ("list", "forget")
DEFAULT_FIELDS = ("id", "repo", "kind", "lean", "status", "confidence", "support", "contra", "n_prs", "path_glob",
                  "language", "category", "statement")
ALL_FIELDS = (*DEFAULT_FIELDS, "first_ts", "last_ts", "expires_ts", "maintainer")
_ID = re.compile(r"[0-9a-f]{16}")


def add_arguments(sub) -> None:
    ls = sub.add_parser("list", help="list the lessons in memory.store (read-only)")
    ls.add_argument("--repo", help="only lessons for this repository (owner/name)")
    ls.add_argument("--status", choices=STATUSES, help="only lessons with this status")
    ls.add_argument("--json", dest="fields", metavar="FIELDS",
                    help=f"comma-separated fields to print per lesson (default: all but timestamps; any of {', '.join(ALL_FIELDS)})")
    fg = sub.add_parser("forget", help="delete a lesson and its evidence; later consolidations do not bring it back")
    fg.add_argument("lesson_id", help="the id `memory list` shows")


def _fields(spec: str | None) -> tuple[str, ...]:
    if spec is None:
        return DEFAULT_FIELDS
    wanted = tuple(f.strip() for f in spec.split(",") if f.strip())
    unknown = [f for f in wanted if f not in ALL_FIELDS]
    if not wanted or unknown:
        raise CodeCheckError(f"--json takes a comma-separated list of: {', '.join(ALL_FIELDS)}"
                             + (f" (unknown: {', '.join(unknown)})" if unknown else ""))
    return wanted


def _row(le: Lesson, fields: tuple[str, ...]) -> dict:
    flat = {"repo": le.scope.repo, "path_glob": le.scope.path_glob, "language": le.scope.language,
            "category": le.scope.category}
    return {f: flat[f] if f in flat else getattr(le, f) for f in fields}


def run(args, ctx) -> dict:
    db = MemoryDB(expand_path(ctx.config.memory.store))
    if args.memory_cmd == "list":
        fields = _fields(args.fields)
        lessons = db.lessons(repo=args.repo, status=args.status)
        return {"store": str(db.file), "count": len(lessons), "lessons": [_row(le, fields) for le in lessons]}
    lesson_id = args.lesson_id
    if not _ID.fullmatch(lesson_id):
        raise CodeCheckError("a lesson id is 16 lowercase hex characters; `memory list` shows them")
    if db.lesson(lesson_id) is None and not db.evidence(lesson_id=lesson_id):
        raise CodeCheckError(f"no lesson {lesson_id} in {db.file}")
    gone = db.forget(lesson_id)
    return {"store": str(db.file), "forgotten": lesson_id, "lesson_removed": gone["lesson"],
            "evidence_removed": gone["evidence"]}
