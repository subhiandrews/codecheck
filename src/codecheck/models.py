"""Provider-independent data types shared by every layer."""

from dataclasses import asdict, dataclass
from enum import StrEnum


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
    def parse(cls, text: str) -> PRRef:
        repo, sep, num = text.strip().rpartition("#")
        if not sep or repo.count("/") != 1 or not num.isdigit():
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

    def to_dict(self) -> dict:
        d = asdict(self)
        d["severity"] = self.severity.value
        d["reviewers"] = list(self.reviewers)
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
