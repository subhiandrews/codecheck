"""What may leave this machine, and in which shape. Pure functions, no I/O.

Three sharing levels, each a superset of the one before:

* `lessons`  : each lesson with its evidence, without `quote` or `finding` (ids, scope, signal, role, PR, thread, time).
* `evidence` : adds a scrubbed `quote` (at most 240 characters, at most 3 per lesson) and `finding{severity, message}`
               (message at most 400 characters).
* `episodes` : adds scrubbed shadow records per run, never the diff.

The effective level for a repository is the minimum of the client's `share_level`, the server's maximum and a cap for
private repositories. A repository whose visibility is unknown counts as private. Everything is scrubbed (`shadow/scrub.py`)
before it is built, and the server validates the same shapes again: it never trusts the client's idea of the level.
"""

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence

from codecheck.config import MEMORY_LEVELS
from codecheck.errors import CodeCheckError
from codecheck.memory.consolidate import POLARITY
from codecheck.memory.evidence import MESSAGE_CAP, QUOTE_CAP
from codecheck.memory.model import Evidence, Scope, evidence_id, lesson_id
from codecheck.shadow.roles import ROLES
from codecheck.shadow.scrub import scrub, scrub_text

LEVELS = MEMORY_LEVELS  # ("lessons", "evidence", "episodes"), lowest first
MAX_QUOTES_PER_LESSON = 3
MAX_EVIDENCE_PER_ITEM = 200
MAX_BATCH_ITEMS = 100
MAX_BODY_BYTES = 256 * 1024
MAX_EPISODE_BYTES = 200 * 1024
MAX_EPISODES_PER_BATCH = 20
DAY = 86400.0

# Keys that would carry code or a diff; an episode record holding one is refused, whatever the level.
DIFF_KEYS = frozenset({"diff", "patch", "raw_diff", "hunks"})
# What a client may attach to a lesson that the server ignores: it recomputes every number from the evidence rows.
IGNORED_LESSON_KEYS = frozenset({"statement", "support", "contra", "n_prs", "confidence", "status", "first_ts", "last_ts",
                                 "expires_ts", "lean", "maintainer", "provenance"})
_EVIDENCE_KEYS = frozenset({"scope", "signal", "role", "pr", "thread", "ts", "tool_version", "quote", "finding", "id", "lesson_id",
                            "vector"})
_LEVEL_B_KEYS = frozenset({"quote", "finding", "vector"})  # a vector is made from text, so it is shared where text is
_SCOPE_KEYS = ("host", "repo", "path_glob", "language", "category")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_HOST = re.compile(r"[A-Za-z0-9.-]{1,100}")
_REPO = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.-]*(/[A-Za-z0-9_.][A-Za-z0-9_.-]*)+")
_SMALL = re.compile(r"[A-Za-z0-9_.+#*/-]{0,40}")
_VERSION = re.compile(r"[A-Za-z0-9_.+-]{1,40}")


class PayloadError(CodeCheckError):
    """A payload that is malformed. `code` is the HTTP status the server answers with."""

    def __init__(self, message: str, code: int = 422) -> None:
        super().__init__(message)
        self.code = code


class LevelError(PayloadError):
    """A payload that carries a field above the sharing level allowed."""


def rank(level: str) -> int:
    return LEVELS.index(level) if level in LEVELS else 0


def effective_level(client_level: str, server_max: str, private_cap: str, repo_private: bool | None) -> str:
    """The lowest of the client's level, the server's maximum and (for a private or unknown repo) the private cap."""
    caps = [client_level, server_max]
    if repo_private is not False:  # True, or None: unknown counts as private
        caps.append(private_cap)
    return min(caps, key=rank)


# ---------- building payloads (client side) ----------

def _clip(text: object, cap: int) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return None
    return scrub_text(text.strip())[:cap]


def scope_payload(scope: Scope) -> dict:
    return dict(zip(_SCOPE_KEYS, scope.key(), strict=True))


def evidence_payload(ev: Evidence, level: str, *, quote: bool = True) -> dict:
    """One evidence row at `level`. `quote` False drops the quote (the lesson already has its 3)."""
    out: dict = {"scope": scope_payload(ev.scope), "signal": ev.signal, "role": ev.role, "pr": ev.pr, "thread": ev.thread,
                 "ts": float(ev.ts)}
    if ev.tool_version:
        out["tool_version"] = ev.tool_version
    if rank(level) >= rank("evidence"):
        q = _clip(ev.quote, QUOTE_CAP) if quote else None
        if q:
            out["quote"] = q
        if ev.finding:
            msg = _clip(ev.finding.get("message"), MESSAGE_CAP)
            sev = ev.finding.get("severity")
            out["finding"] = {"severity": sev if isinstance(sev, str) else None, "message": msg}
    return out


def lesson_item(lid: str, kind: str, evidence: Sequence[Evidence], level: str) -> dict:
    """A lesson with its evidence. Only counts-free identity goes out: the server derives every number itself."""
    ordered = sorted(evidence, key=lambda e: (-e.ts, e.id))
    allowed = {e.id for e in ordered if e.quote}
    quoted = [e.id for e in ordered if e.id in allowed][:MAX_QUOTES_PER_LESSON]
    rows = [evidence_payload(e, level, quote=e.id in quoted) for e in sorted(evidence, key=lambda e: (e.ts, e.id))]
    return {"lesson_id": lid, "kind": kind, "evidence": rows[-MAX_EVIDENCE_PER_ITEM:]}


def episode_payload(run_id: str, repo: str, pr: str, records: Sequence[Mapping]) -> dict:
    """Scrubbed shadow records for one run. Anything that could be a diff is removed."""
    keep = [scrub({k: v for k, v in r.items() if k not in DIFF_KEYS}) for r in records if isinstance(r, Mapping)]
    return {"run_id": run_id, "repo": repo, "pr": pr, "records": keep}


def digest(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:24]


# ---------- validating payloads (server side, and the client when it reads a pull) ----------

def _text(value: object, name: str, cap: int, *, pattern: re.Pattern | None = None, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty) or len(value) > cap or _CONTROL.search(value):
        raise PayloadError(f"{name} must be a short single-line string")
    if pattern is not None and not pattern.fullmatch(value):
        raise PayloadError(f"{name} has unexpected characters")
    return value


def parse_scope(raw: object) -> Scope:
    if not isinstance(raw, dict) or set(raw) != set(_SCOPE_KEYS):
        raise PayloadError("scope must have exactly host, repo, path_glob, language and category")
    host = _text(raw["host"], "scope.host", 100, pattern=_HOST)
    repo = _text(raw["repo"], "scope.repo", 200, pattern=_REPO)
    glob = _text(raw["path_glob"], "scope.path_glob", 200)
    if glob.startswith(("/", "-")) or ".." in glob.split("/"):
        raise PayloadError("scope.path_glob must be a relative pattern")
    language = _text(raw["language"], "scope.language", 40, pattern=_SMALL, allow_empty=True)
    category = _text(raw["category"], "scope.category", 40, pattern=_SMALL)
    return Scope(host, repo, glob, language, category)


def parse_evidence(raw: object, max_level: str, now: float) -> Evidence:
    """A validated Evidence whose ids are derived here, never taken from the sender. Raises LevelError above `max_level`."""
    if not isinstance(raw, dict):
        raise PayloadError("evidence must be an object")
    unknown = set(raw) - _EVIDENCE_KEYS
    if unknown:
        raise PayloadError(f"unknown evidence field {min(unknown)!r}")
    above = [k for k in _LEVEL_B_KEYS if raw.get(k) is not None]
    if above and rank(max_level) < rank("evidence"):
        raise LevelError(f"field {above[0]!r} is above this server's maximum sharing level ({max_level})")
    scope = parse_scope(raw.get("scope"))
    signal = raw.get("signal")
    if not isinstance(signal, str) or signal not in POLARITY:
        raise PayloadError("unknown signal")
    role = raw.get("role")
    if not isinstance(role, str) or role not in ROLES:
        raise PayloadError("unknown role")
    pr = _text(raw.get("pr"), "pr", 200)
    thread = _text(raw.get("thread", ""), "thread", 200, allow_empty=True)
    ts = raw.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts) or ts <= 0 or ts > now + DAY:
        raise PayloadError("ts must be a past time in seconds")
    version = raw.get("tool_version")
    if version is not None:
        version = _text(version, "tool_version", 40, pattern=_VERSION)
    quote = raw.get("quote")
    if quote is not None:
        if not isinstance(quote, str):
            raise PayloadError("quote must be text")
        quote = _clip(quote, QUOTE_CAP)
    finding = parse_finding(raw.get("finding"))
    if raw.get("vector") is not None and not (quote or (finding or {}).get("message")):
        raise PayloadError("a vector needs the evidence text it was made from")
    kind = "miss" if signal == "human_miss" else "finding"
    return Evidence(
        id=evidence_id(scope.host, scope.repo, pr, thread, signal), lesson_id=lesson_id(scope, kind), scope=scope,
        signal=signal, ts=float(ts), pr=pr, thread=thread, role=role, tool_version=version, quote=quote, finding=finding)


def parse_finding(raw: object) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - {"severity", "message"}:
        raise PayloadError("finding must hold only severity and message")
    sev, msg = raw.get("severity"), raw.get("message")
    if sev is not None and (not isinstance(sev, str) or len(sev) > 20 or _CONTROL.search(sev)):
        raise PayloadError("finding.severity must be a short string")
    if msg is not None and not isinstance(msg, str):
        raise PayloadError("finding.message must be text")
    return {"severity": sev, "message": _clip(msg, MESSAGE_CAP)}


def parse_item(raw: object, max_level: str, now: float) -> tuple[str, list[Evidence]]:
    """One batch item -> (lesson id, evidence). The claimed lesson id must equal what the evidence derives."""
    if not isinstance(raw, dict):
        raise PayloadError("item must be an object")
    unknown = set(raw) - {"lesson_id", "kind", "evidence"} - IGNORED_LESSON_KEYS
    if unknown:
        raise PayloadError(f"unknown lesson field {min(unknown)!r}")
    evidence = raw.get("evidence")
    if not isinstance(evidence, list) or not evidence or len(evidence) > MAX_EVIDENCE_PER_ITEM:
        raise PayloadError(f"evidence must be a list of 1 to {MAX_EVIDENCE_PER_ITEM} items")
    claimed = raw.get("lesson_id")
    if not isinstance(claimed, str):
        raise PayloadError("lesson_id must be text")
    parsed = [parse_evidence(e, max_level, now) for e in evidence]
    if any(e.lesson_id != claimed for e in parsed):
        raise PayloadError("evidence does not belong to the lesson it was sent under")
    if sum(1 for e in parsed if e.quote) > MAX_QUOTES_PER_LESSON:
        raise PayloadError(f"at most {MAX_QUOTES_PER_LESSON} quotes per lesson")
    return claimed, parsed


def parse_episode(raw: object) -> dict:
    """A validated episode: run id, repo, PR and scrubbed records, with no diff. Raises PayloadError."""
    from codecheck.runids import check_run_id

    if not isinstance(raw, dict) or set(raw) != {"run_id", "repo", "pr", "records"}:
        raise PayloadError("episode must hold exactly run_id, repo, pr and records")
    try:
        run_id = check_run_id(raw["run_id"])
    except CodeCheckError:
        raise PayloadError("invalid run id") from None
    repo = _text(raw["repo"], "repo", 200, pattern=_REPO)
    pr = _text(raw["pr"], "pr", 200)
    records = raw["records"]
    if not isinstance(records, list) or len(records) > 5000 or not all(isinstance(r, dict) for r in records):
        raise PayloadError("records must be a list of objects")
    if any(k in DIFF_KEYS for r in records for k in r):
        raise PayloadError("an episode must not carry a diff")
    body = {"run_id": run_id, "repo": repo, "pr": pr, "records": scrub(records)}
    if len(json.dumps(body)) > MAX_EPISODE_BYTES:
        raise PayloadError("episode is too large", 413)
    return body
