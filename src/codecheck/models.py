"""Provider-independent data types shared by every layer."""

import re
from dataclasses import asdict, dataclass
from enum import StrEnum

_BOT_SUFFIX = re.compile(r"\[bot\]$", re.IGNORECASE)
_BOT_NAME = re.compile(r"[-_]bot$", re.IGNORECASE)
# Accounts that review automatically but carry no bot flag on their host.
_AUTOMATED_LOGINS = frozenset({"gitlabduo", "gitlab-duo"})


def is_service_login(login: str) -> bool:
    """A login that is certainly automated: `name[bot]` or a known AI reviewer such as GitLab Duo."""
    return bool(_BOT_SUFFIX.search(login)) or login.lower() in _AUTOMATED_LOGINS


def same_login(author: str, me: str, is_bot: bool = False) -> bool:
    """Whether `author` is `me`. GitHub's REST API (and `review.login`) spell a bot `name[bot]`, but GraphQL, which
    supplies thread comments, drops the suffix; a bare login that equals ours minus the suffix is us too, but only
    when the host says the author is a bot (users and apps have separate names, so a person may be called `name`)."""
    if not me:
        return False
    return author == me or (is_bot and author == _BOT_SUFFIX.sub("", me))


OWN_MARKER = "<!-- code-check:"  # starts every hidden marker we put in a comment (finding, reply, run)


def comment_is_ours(c: ThreadComment, me: str) -> bool:
    """Whether a thread comment is ours. Under our exact login it is. A bot's bare GraphQL login is shared by every
    workflow that uses the same token, so it counts only when the comment carries one of our hidden markers."""
    return c.author == me or (same_login(c.author, me, c.is_bot) and OWN_MARKER in c.body)


def same_pr_author(author: str, me: str) -> bool:
    """Whether a PR's author is `me`. `gh` spells a bot author `app/name`, while `review.login` says `name[bot]`."""
    if not me:
        return False
    return author == me or (me.lower().endswith("[bot]") and author == "app/" + _BOT_SUFFIX.sub("", me))


def is_bot_login(login: str) -> bool:
    """Wider, for sorting feedback only: also `name-bot` and `name_bot`, which a person could own. Never use it to
    drop someone's reply from a review decision."""
    return is_service_login(login) or bool(_BOT_NAME.search(login))


class Severity(StrEnum):
    CRITICAL = "critical"
    WARNING = "warning"
    SUGGESTION = "suggestion"

    @property
    def rank(self) -> int:
        return {"critical": 3, "warning": 2, "suggestion": 1}[self.value]

    @property
    def label(self) -> str:
        return self.value.capitalize()


@dataclass(frozen=True, slots=True)
class PRRef:
    repo: str
    number: int

    def __str__(self) -> str:
        return f"{self.repo}#{self.number}"

    @classmethod
    def parse(cls, text: str, nested: bool = False) -> PRRef:
        """`nested` allows a namespace of any depth (GitLab subgroups); the default is exactly OWNER/REPO."""
        repo, sep, num = text.strip().rpartition("#")
        if not sep or not num.isdigit() or (repo.count("/") < 1 if nested else repo.count("/") != 1):
            raise ValueError(f"expected OWNER/REPO#NUMBER, got {text!r}")
        return cls(repo, int(num))


@dataclass(frozen=True, slots=True)
class PullRequest:
    ref: PRRef
    title: str
    body: str
    author: str
    base_ref: str
    head_ref: str
    head_sha: str
    is_draft: bool
    state: str  # "open" | "closed" | "merged"
    url: str
    is_cross_repo: bool = False


@dataclass(frozen=True, slots=True)
class ThreadComment:
    id: int
    author: str
    body: str
    created_at: str
    reactions: tuple[str, ...] = ()
    is_bot: bool = False


@dataclass(frozen=True, slots=True)
class Review:
    author: str
    commit_id: str
    submitted_at: str
    state: str
    body: str = ""
    is_bot: bool = False


@dataclass(frozen=True, slots=True)
class IssueComment:
    author: str
    body: str
    created_at: str
    is_bot: bool = False


@dataclass(frozen=True, slots=True)
class Thread:
    id: str
    path: str
    line: int | None
    is_resolved: bool
    is_outdated: bool
    comments: tuple[ThreadComment, ...]


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    state: str  # "pass" | "fail" | "pending" | "skipped" | "cancelled"
    link: str = ""
    run_id: str | None = None


@dataclass(frozen=True, slots=True)
class Finding:
    file: str
    line: int
    severity: Severity
    category: str
    message: str
    suggestion: str | None = None
    reviewers: tuple[str, ...] = ()
    anchor: str = ""
    rated_because: str | None = None  # the aggregator's reasoning about the severity; recorded, never posted

    def to_dict(self) -> dict:
        d = asdict(self)
        d["severity"] = self.severity.value
        d["reviewers"] = list(self.reviewers)
        if d["rated_because"] is None:
            del d["rated_because"]
        return d


@dataclass(frozen=True, slots=True)
class ReviewComment:
    path: str
    line: int
    side: str
    body: str


@dataclass(frozen=True, slots=True)
class Decision:
    event: str  # "APPROVE" | "COMMENT"
    reason: str


@dataclass(frozen=True, slots=True)
class PostResult:
    posted: int
    degraded: bool = False
