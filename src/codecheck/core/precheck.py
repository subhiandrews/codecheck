"""Decide, without any LLM call, whether there is anything worth reviewing."""

from collections.abc import Sequence
from dataclasses import dataclass

from codecheck.core.panel import filter_ignored
from codecheck.core.state import StateStore
from codecheck.models import PullRequest, Review, Thread
from codecheck.shadow.scrub import scrub_text


@dataclass(frozen=True, slots=True)
class PrecheckResult:
    proceed: bool
    reason: str
    files: tuple[str, ...] = ()
    fingerprint: str = ""


def _skip(reason: str) -> PrecheckResult:
    return PrecheckResult(False, reason)


def precheck_pr(
    pr: PullRequest, files: Sequence[str], state: StateStore, *, ignore: Sequence[str], me: str, force: bool = False
) -> PrecheckResult:
    if pr.state != "open":
        return _skip(f"PR is {pr.state}")
    if pr.is_draft:
        return _skip("PR is a draft")
    if pr.author == me:
        return _skip("own PR: use address-comments instead")
    reviewable = filter_ignored(files, ignore)
    if not reviewable:
        return _skip("no reviewable files after ignore patterns")
    if not force and state.get(f"review:{pr.ref}") == pr.head_sha:
        return _skip(f"head {pr.head_sha[:8]} already reviewed")
    return PrecheckResult(True, "ok", tuple(reviewable), pr.head_sha)


def precheck_local(
    files: Sequence[str], fingerprint: str, state_key: str, state: StateStore, *, ignore: Sequence[str], force: bool = False
) -> PrecheckResult:
    if not files:
        return _skip("no changes against base")
    reviewable = filter_ignored(files, ignore)
    if not reviewable:
        return _skip("no reviewable files after ignore patterns")
    if not force and state.get(state_key) == fingerprint:
        return _skip("unchanged since last review")
    return PrecheckResult(True, "ok", tuple(reviewable), fingerprint)


@dataclass(frozen=True, slots=True)
class Reply:
    thread_id: str
    path: str
    line: int | None
    author: str
    body: str
    created_at: str


@dataclass(frozen=True, slots=True)
class Activity:
    last_review: Review | None
    replies: tuple[Reply, ...]


def review_activity(reviews: Sequence[Review], threads: Sequence[Thread], me: str) -> Activity:
    """Our latest review and the human replies on our threads since it (resolved and outdated threads count)."""
    mine = [r for r in reviews if r.author == me]
    if not mine:
        return Activity(None, ())
    last = max(mine, key=lambda r: r.submitted_at)
    replies = []
    for t in threads:
        if not t.comments or t.comments[0].author != me:
            continue
        for c in t.comments[1:]:
            if c.author == me or c.is_bot or c.author.endswith("[bot]"):
                continue
            if c.created_at > last.submitted_at:
                replies.append(Reply(t.id, t.path, t.line, c.author, c.body, c.created_at))
    return Activity(last, tuple(replies))


@dataclass(frozen=True, slots=True)
class Verdict:
    kind: str  # "new" | "retry" | "replies" | "reviewed"
    reason: str
    replies: tuple[Reply, ...] = ()


def review_verdict(pr: PullRequest, state: StateStore, activity: Activity | None) -> Verdict:
    if state.get(f"review-incomplete:{pr.ref}") == pr.head_sha:
        return Verdict("retry", "the previous review of this head was incomplete")
    reviewed = state.get(f"review:{pr.ref}")
    if not reviewed and activity and activity.last_review and activity.last_review.commit_id:
        reviewed = activity.last_review.commit_id
    if not reviewed:
        return Verdict("new", "ok")
    if reviewed != pr.head_sha:
        return Verdict("new", "new commits since your review")
    if activity and activity.replies:
        n = len(activity.replies)
        threads = len({r.thread_id for r in activity.replies})
        return Verdict(
            "replies",
            f"{n} repl{'y' if n == 1 else 'ies'} on {threads} of your threads, no new commits since your review",
            activity.replies,
        )
    return Verdict("reviewed", f"head {pr.head_sha[:8]} already reviewed")


def needs_activity(pr: PullRequest, state: StateStore) -> bool:
    """True when GitHub activity could change the verdict, so fetching it is worthwhile."""
    if state.get(f"review-incomplete:{pr.ref}") == pr.head_sha:
        return False
    local = state.get(f"review:{pr.ref}")
    return not local or local == pr.head_sha


def cap_replies(replies: Sequence[Reply], max_threads: int = 20, max_chars: int = 1000) -> tuple[Reply, ...]:
    allowed: list[str] = []
    out = []
    for r in replies:
        if r.thread_id not in allowed:
            if len(allowed) >= max_threads:
                continue
            allowed.append(r.thread_id)
        out.append(Reply(r.thread_id, r.path, r.line, r.author, scrub_text(r.body[:max_chars]), r.created_at))
    return tuple(out)
