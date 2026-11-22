"""Lessons and the evidence behind them. Ids are stable hashes, so any two machines derive the same id."""

import hashlib
from dataclasses import dataclass, field

from codecheck.shadow.roles import ROLES

# A finding scope is one `finding` lesson whatever the evidence says: whether it leans dispute or confirm is derived
# for display (`Lesson.lean`) and is never part of the id, so the id does not change when the majority flips.
KINDS = ("miss", "finding")
LEANS = ("confirm", "dispute")
POLARITIES = ("for", "against", "miss", "neutral")
STATUSES = ("candidate", "active", "contested", "expired")
DEFAULT_HOST = "github.com"
UNKNOWN = "unknown"  # the role of evidence recorded before roles existed


def _hash(*parts: object) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


def clean_role(value: object) -> str:
    return value if isinstance(value, str) and value in ROLES else UNKNOWN


@dataclass(frozen=True)
class Scope:
    host: str
    repo: str
    path_glob: str
    language: str  # "" when the language is not recognised
    category: str  # a canonical category, or "any" for a miss (human comments have none)

    def key(self) -> tuple[str, str, str, str, str]:
        return (self.host, self.repo, self.path_glob, self.language, self.category)


def lesson_id(scope: Scope, kind: str) -> str:
    """The same scope and kind give the same id on every machine."""
    return _hash("lesson", *scope.key(), kind)


def evidence_id(host: str, repo: str, pr: str, thread_id: str, signal: str) -> str:
    """A thread seen twice is one row."""
    return _hash("evidence", host, repo, pr, thread_id, signal)


def thread_key(host: str, repo: str, pr: str, thread_id: str) -> str:
    """One thread, whatever signal it currently shows: a newer signal replaces an older one for the same thread."""
    return _hash("thread", host, repo, pr, thread_id)


@dataclass(frozen=True)
class Evidence:
    id: str
    lesson_id: str
    scope: Scope
    signal: str
    ts: float
    pr: str
    thread: str = ""
    role: str = UNKNOWN  # `unknown` for evidence recorded before roles existed
    tool_version: str | None = None
    quote: str | None = None  # kept locally only, scrubbed and capped
    finding: dict | None = None  # kept locally only, scrubbed and capped: {severity, message}

    def thread_key(self) -> str:
        return thread_key(self.scope.host, self.scope.repo, self.pr, self.thread)


@dataclass(frozen=True)
class Lesson:
    id: str
    scope: Scope
    kind: str  # miss | finding
    lean: str  # confirm | dispute for a finding (derived, display only); "" for a miss
    statement: str
    support: float  # weight for: confirmations (finding) or misses (miss)
    contra: float  # weight against; 0 for a miss
    n_prs: int  # distinct PRs on the leading side
    confidence: float  # leading weight over total weight
    status: str  # candidate | active | contested | expired
    first_ts: float
    last_ts: float
    expires_ts: float
    maintainer: bool = False  # the leading side has an item from a non-author maintainer
    provenance: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Query:
    """One piece of query text for similarity lookup, with the context it came from."""
    text: str
    path: str = ""
    category: str = ""
    language: str = ""
