"""Lessons and the evidence behind them. Ids are stable hashes, so any two machines derive the same id."""

import hashlib
from dataclasses import dataclass, field

KINDS = ("miss", "dispute", "confirm")
POLARITIES = ("for", "against", "miss", "neutral")
STATUSES = ("candidate", "active", "contested", "expired")
DEFAULT_HOST = "github.com"


def _hash(*parts: object) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


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


@dataclass(frozen=True)
class Evidence:
    id: str
    lesson_id: str
    scope: Scope
    polarity: str  # for | against | miss | neutral
    signal: str
    weight: float  # before decay
    ts: float
    pr: str
    role: str | None = None  # not recorded yet, so always None today
    tool_version: str | None = None
    quote: str | None = None  # kept locally only
    finding: dict | None = None  # kept locally only


@dataclass(frozen=True)
class Lesson:
    id: str
    scope: Scope
    kind: str  # miss | dispute | confirm
    statement: str
    support: float
    contra: float
    n_prs: int
    confidence: float
    status: str  # candidate | active | contested | expired
    first_ts: float
    last_ts: float
    expires_ts: float
    unweighted: bool = True  # roles are not recorded, so no weight reflects who said it
    provenance: dict = field(default_factory=dict)
