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
    is_mine: bool = False


@dataclass(frozen=True, slots=True)
class ExistingThread:
    thread_id: str
    path: str
    line: int | None
    resolved: bool
    outdated: bool
    comments: tuple[ExistingComment, ...]


def collect_existing(
    threads: Sequence[Thread], activity: Activity, *, me: str = "", max_threads: int = 20, max_chars: int = 1000,
    max_total: int = 100,
) -> tuple[ExistingThread, ...]:
    """Human comments per thread, scrubbed then truncated. Threads with replies since our review come first, then
    unresolved ones, so a cap never cuts what changed; `me` marks our own earlier comments."""
    new = {(r.thread_id, r.created_at, r.author) for r in activity.replies}
    out: list[ExistingThread] = []
    total = 0
    fresh = {thread_id for thread_id, _, _ in new}
    for t in sorted(threads, key=lambda t: (t.id not in fresh, t.is_resolved)):
        if len(out) >= max_threads or total >= max_total:
            break
        comments = tuple(
            ExistingComment(c.author, scrub_text(c.body)[:max_chars], c.created_at, (t.id, c.created_at, c.author) in new,
                            bool(me) and c.author == me)
            for c in t.comments
            if not (c.is_bot or c.author.endswith("[bot]"))
        )[: max_total - total]
        if comments:
            out.append(ExistingThread(t.id, t.path, t.line, t.is_resolved, t.is_outdated, comments))
            total += len(comments)
    return tuple(out)
