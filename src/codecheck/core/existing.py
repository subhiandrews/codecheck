"""A PR's existing human review threads, shaped for reviewers (no LLM)."""

from collections.abc import Sequence
from dataclasses import dataclass

from codecheck.core.precheck import Activity
from codecheck.models import Thread
from codecheck.shadow.scrub import scrub_text


@dataclass(frozen=True, slots=True)
class ExistingComment:
    author: str
    body: str
    created_at: str
    is_new: bool


@dataclass(frozen=True, slots=True)
class ExistingThread:
    thread_id: str
    path: str
    line: int | None
    resolved: bool
    outdated: bool
    comments: tuple[ExistingComment, ...]


def collect_existing(
    threads: Sequence[Thread], activity: Activity, *, max_threads: int = 20, max_chars: int = 1000, max_total: int = 100
) -> tuple[ExistingThread, ...]:
    """Human comments per thread, unresolved first, scrubbed then truncated; replies since our review are tagged new."""
    new = {(r.thread_id, r.created_at, r.author) for r in activity.replies}
    out: list[ExistingThread] = []
    total = 0
    for t in sorted(threads, key=lambda t: t.is_resolved):
        if len(out) >= max_threads or total >= max_total:
            break
        comments = tuple(
            ExistingComment(c.author, scrub_text(c.body)[:max_chars], c.created_at, (t.id, c.created_at, c.author) in new)
            for c in t.comments
            if not (c.is_bot or c.author.endswith("[bot]"))
        )[: max_total - total]
        if comments:
            out.append(ExistingThread(t.id, t.path, t.line, t.is_resolved, t.is_outdated, comments))
            total += len(comments)
    return tuple(out)
