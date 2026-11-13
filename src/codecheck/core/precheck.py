"""Decide, without any LLM call, whether there is anything worth reviewing."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from codecheck.core.panel import filter_ignored
from codecheck.core.state import StateStore
from codecheck.models import PullRequest, Review, Thread
from codecheck.shadow.scrub import scrub_text


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


def approved_by(reviews: Sequence[Review], head_sha: str) -> str | None:
    """A reviewer whose latest approving/blocking/dismissing review is an approval of this head."""
    latest: dict[str, Review] = {}
    for r in reviews:
        if r.state not in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED") or not r.submitted_at:
            continue  # comments do not change a verdict; pending reviews have no submit time
        if r.author not in latest or r.submitted_at >= latest[r.author].submitted_at:
            latest[r.author] = r
    for author in sorted(latest):
        if latest[author].state == "APPROVED" and latest[author].commit_id == head_sha:
            return author
    return None


@dataclass(frozen=True, slots=True)
class Verdict:
    kind: str  # "new" | "retry" | "replies" | "reviewed"
    reason: str
    replies: tuple[Reply, ...] = ()


def replies_seen_key(pr: PullRequest) -> str:
    return f"replies-seen:{pr.ref}"


def review_verdict(pr: PullRequest, state: StateStore, activity: Activity | None) -> Verdict:
    if state.get(f"review-incomplete:{pr.ref}") == pr.head_sha:
        return Verdict("retry", "the previous review of this head was incomplete")
    reviewed = state.get(f"review:{pr.ref}")
    theirs = activity.last_review.commit_id if activity and activity.last_review else ""
    if theirs == pr.head_sha or not reviewed:
        reviewed = theirs or reviewed  # GitHub showing our review at the head beats a stale local record
    if not reviewed:
        return Verdict("new", "ok")
    if reviewed != pr.head_sha:
        return Verdict("new", "new commits since your review")
    seen = state.get(replies_seen_key(pr)) or ""
    fresh = tuple(r for r in activity.replies if r.created_at > seen) if activity else ()
    if fresh:
        n = len(fresh)
        threads = len({r.thread_id for r in fresh})
        return Verdict(
            "replies",
            f"{n} repl{'y' if n == 1 else 'ies'} on {threads} of your threads, no new commits since your review",
            fresh,
        )
    return Verdict("reviewed", f"head {pr.head_sha[:8]} already reviewed")


def needs_activity(pr: PullRequest, state: StateStore) -> bool:
    """True unless a retry is forced: GitHub may show our review at the head even when local state differs."""
    return state.get(f"review-incomplete:{pr.ref}") != pr.head_sha


def fetch_activity(
    pr: PullRequest,
    state: StateStore,
    me: str,
    list_reviews: Callable[[], Sequence[Review]],
    list_threads: Callable[[], Sequence[Thread]],
) -> Activity | None:
    """Our latest review and replies on our threads, fetching only what could change the verdict."""
    if not needs_activity(pr, state):
        return None
    reviews = list_reviews()
    last = review_activity(reviews, [], me).last_review
    if last is None:
        return Activity(None, ())  # no review of ours: there is nothing to reply to
    if last.commit_id and last.commit_id != pr.head_sha and state.get(f"review:{pr.ref}") != pr.head_sha:
        return Activity(last, ())  # a new commit is certain, so replies cannot change the verdict
    return review_activity(reviews, list_threads(), me)


def cap_replies(
    replies: Sequence[Reply], max_threads: int = 20, max_chars: int = 1000, max_total: int = 100
) -> tuple[Reply, ...]:
    allowed: list[str] = []
    out = []
    for r in replies:
        if len(out) >= max_total:
            break
        if r.thread_id not in allowed:
            if len(allowed) >= max_threads:
                continue
            allowed.append(r.thread_id)
        out.append(Reply(r.thread_id, r.path, r.line, r.author, scrub_text(r.body)[:max_chars], r.created_at))
    return tuple(out)


@dataclass(frozen=True, slots=True)
class PrecheckResult:
    proceed: bool
    reason: str
    files: tuple[str, ...] = ()
    fingerprint: str = ""
    replies: tuple[Reply, ...] = ()


def _skip(reason: str) -> PrecheckResult:
    return PrecheckResult(False, reason)


def cheap_skip(pr: PullRequest, files: Sequence[str], *, ignore: Sequence[str], me: str) -> PrecheckResult | None:
    """The skips that need no GitHub activity, so callers can avoid fetching it."""
    if pr.state != "open":
        return _skip(f"PR is {pr.state}")
    if pr.is_draft:
        return _skip("PR is a draft")
    if pr.author == me:
        return _skip("own PR: use address-comments instead")
    if not filter_ignored(files, ignore):
        return _skip("no reviewable files after ignore patterns")
    return None


def precheck_pr(
    pr: PullRequest, files: Sequence[str], state: StateStore, *, ignore: Sequence[str], me: str, force: bool = False,
    activity: Activity | None = None,
) -> PrecheckResult:
    skip = cheap_skip(pr, files, ignore=ignore, me=me)
    if skip:
        return skip
    reviewable = filter_ignored(files, ignore)
    if not force:
        verdict = review_verdict(pr, state, activity)
        if verdict.kind in ("replies", "reviewed"):
            return PrecheckResult(False, verdict.reason, replies=verdict.replies)
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
