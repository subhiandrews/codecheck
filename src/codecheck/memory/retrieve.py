"""Deterministic exact-key retrieval of lessons for a review. No model, no embedding, no network.

Miss lessons are keyed on the diff (repo, path glob, language; a miss has no category). Finding lessons are keyed on the
candidate findings (repo, path glob, canonical category, language). Only `active` and `contested` lessons are ever
returned: a candidate has too little evidence to reach a review. A lesson is checked before use: its path glob must still
match at least one file at the base commit, so a lesson about code that no longer exists is not shown.
"""

import time
from collections.abc import Callable, Iterable, Sequence

from codecheck.core.panel import detect_language
from codecheck.memory.categories import ANY, canonical_category
from codecheck.memory.db import MemoryDB
from codecheck.memory.evidence import path_glob
from codecheck.memory.model import Lesson

SHOWN = ("active", "contested")
_ORDER = {"active": 0, "contested": 1}


def glob_matches(glob: str, files: Iterable[str]) -> bool:
    """`dir/sub/**` matches any file below that prefix; `*` matches a file at the top level. Exactly what `path_glob` makes."""
    if glob == "*":
        return any("/" not in f for f in files)
    if glob.endswith("/**"):
        prefix = glob[:-2]
        return any(f.startswith(prefix) for f in files)
    return False


def _rank(le: Lesson) -> tuple:
    return (_ORDER.get(le.status, 9), -le.confidence, -le.last_ts, le.id)


def _select(candidates: Iterable[Lesson], files_at_base: Sequence[str], limit: int, now: float) -> list[Lesson]:
    seen: set[str] = set()
    out = []
    for le in sorted(candidates, key=_rank):
        if le.id in seen or le.status not in SHOWN or le.expires_ts <= now:
            continue
        if not glob_matches(le.scope.path_glob, files_at_base):
            continue
        seen.add(le.id)
        out.append(le)
    return out[:max(limit, 0)]


def miss_lessons(db: MemoryDB, repo: str, paths: Iterable[str], files_at_base: Sequence[str], limit: int,
                 now: Callable[[], float] = time.time) -> list[Lesson]:
    """Miss lessons for the changed paths: the same repo, path glob and language as one of them."""
    keys = {(path_glob(p), detect_language(p) or "") for p in paths}
    found = [le for le in db.lessons(repo=repo, kind="miss") if (le.scope.path_glob, le.scope.language) in keys]
    return _select(found, files_at_base, limit, now())


def finding_lessons(db: MemoryDB, repo: str, findings: Iterable[tuple[str, object]], files_at_base: Sequence[str],
                    limit: int, now: Callable[[], float] = time.time) -> list[Lesson]:
    """Finding lessons for candidate findings given as (file, category): the same repo, path glob, canonical category and language."""
    keys = {(path_glob(f), canonical_category(c), detect_language(f) or "") for f, c in findings}
    keys = {k for k in keys if k[1] != ANY}
    found = [le for le in db.lessons(repo=repo, kind="finding")
             if (le.scope.path_glob, le.scope.category, le.scope.language) in keys]
    return _select(found, files_at_base, limit, now())
