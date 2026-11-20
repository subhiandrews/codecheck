"""Work queue over the shadow store's hand-off folders, and the guard for unattended hand-off rounds.

A hand-off is pending until `<run_id>.handoff/analysis.json` says `done`. `claim_next` hands out the oldest
public one and records an attempt, `finish` and `release` close it. The diff guard (`summarize_diff`,
`violations`) decides whether an unattended round may merge: it is deterministic so that a model cannot argue
with it. It must be run from a trusted copy of code-check, never from the branch under test.
"""

import json
import re
import secrets
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from codecheck.core.state import FileLock
from codecheck.errors import CodeCheckError
from codecheck.runids import check_run_id
from codecheck.shadow.scrub import scrub, scrub_text
from codecheck.shadow.store import ShadowStore, write_private

MARKER = "analysis.json"
MAX_ATTEMPTS = 3
CLAIM_STALE_SECONDS = 4 * 3600
MAX_DIFF_LINES = 2000
_PR_URL = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[0-9]+")


# -- the queue -----------------------------------------------------------------------------------------------


def _marker_path(shadow: ShadowStore, run_id: str) -> Path:
    return shadow.root / f"{check_run_id(run_id)}.handoff" / MARKER


def _read_marker(shadow: ShadowStore, run_id: str) -> dict | None:
    path = _marker_path(shadow, run_id)
    if path.is_symlink() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_marker(shadow: ShadowStore, run_id: str, data: dict) -> None:
    shadow.handoff_dir(run_id)  # creates the folder 0700 and refuses a symlinked one
    write_private(_marker_path(shadow, run_id), json.dumps(scrub(data), ensure_ascii=False, sort_keys=True, indent=1))


def _run_record(shadow: ShadowStore, run_id: str) -> dict:
    return next((r for r in shadow.read(run_id) if r.get("type") == "run"), {})


def _skip_reason(shadow: ShadowStore, run_id: str, now: float) -> str | None:
    folder = shadow.root / f"{run_id}.handoff"
    if folder.is_symlink() or not (folder / "handoff.json").is_file():
        return "no_handoff"
    if _run_record(shadow, run_id).get("repo_private") is not False:
        return "not_public"  # private, or visibility unknown: golden cases are committed to a public repository
    marker = _read_marker(shadow, run_id)
    if marker is None:
        return None
    if marker.get("status") == "done":
        return "done"
    attempts = marker.get("attempts") if isinstance(marker.get("attempts"), int) else 0
    claimed_ts = marker.get("claimed_ts")
    claimed_ts = float(claimed_ts) if isinstance(claimed_ts, (int, float)) and not isinstance(claimed_ts, bool) else 0.0
    if marker.get("status") == "claimed" and now - claimed_ts < CLAIM_STALE_SECONDS:
        return "claimed"
    return "gave_up" if attempts >= MAX_ATTEMPTS else None


def claim_next(shadow: ShadowStore, now: float) -> dict:
    """Claim the oldest pending public hand-off. `skipped` counts the runs passed over, by reason."""
    if not shadow.root.exists():
        return {"run": None, "skipped": {}}
    lock = FileLock(shadow.root / "queue.lock", stale_after=600)
    if not lock.acquire():
        return {"run": None, "skipped": {}, "busy": True}
    try:
        skipped: dict[str, int] = {}
        for run_id in shadow.ordered_run_ids():
            reason = _skip_reason(shadow, run_id, now)
            if reason:
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            old = _read_marker(shadow, run_id) or {}
            attempts = (old.get("attempts") if isinstance(old.get("attempts"), int) else 0) + 1
            pr = _run_record(shadow, run_id).get("pr")
            claim = secrets.token_hex(16)  # whoever holds it owns this claim; a re-claim issues a new one
            _write_marker(shadow, run_id, {"status": "claimed", "attempts": attempts, "claimed_ts": now, "pr": pr,
                                           "nonce": claim})
            return {"run": run_id, "pr": pr, "attempt": attempts, "claim": claim,
                    "handoff_dir": str(shadow.root / f"{run_id}.handoff"), "skipped": skipped}
        return {"run": None, "skipped": skipped}
    finally:
        lock.release()


def _claimed_marker(shadow: ShadowStore, run_id: str, claim: str) -> dict:
    marker = _read_marker(shadow, run_id)
    token = marker.get("nonce") if marker else None
    if marker is None or marker.get("status") != "claimed" or not isinstance(token, str) \
            or not secrets.compare_digest(token, claim or ""):
        raise CodeCheckError(f"hand-off {run_id} is not claimed by this token")
    return marker


def require_claim(shadow: ShadowStore, run_id: str, claim: str) -> None:
    _claimed_marker(shadow, check_run_id(run_id), claim)


def finish(shadow: ShadowStore, run_id: str, pr_url: str, now: float, claim: str) -> None:
    check_run_id(run_id)
    if not _PR_URL.fullmatch(pr_url or ""):
        raise CodeCheckError("--pr must be a pull request URL like https://github.com/OWNER/REPO/pull/N")
    marker = _read_marker(shadow, run_id)
    if marker is not None and marker.get("status") == "done":
        return
    marker = _claimed_marker(shadow, run_id, claim)
    _write_marker(shadow, run_id, {**marker, "status": "done", "pr": pr_url, "done_ts": now})


def release(shadow: ShadowStore, run_id: str, reason: str, now: float, claim: str) -> None:
    marker = _claimed_marker(shadow, check_run_id(run_id), claim)
    _write_marker(shadow, run_id, {**marker, "status": "released", "reason": scrub_text(reason or "")[:500],
                                   "released_ts": now})


# -- the diff guard ------------------------------------------------------------------------------------------


@dataclass
class FileChange:
    path: str
    old_path: str | None = None
    status: str = "M"  # A added, D deleted, R renamed, M modified
    mode: str | None = None  # the mode git reports for a new file
    mode_changed: bool = False
    binary: bool = False
    added: int = 0
    removed: int = 0
    added_lines: list[str] = field(default_factory=list)
    removed_lines: list[str] = field(default_factory=list)


_HEADER = re.compile(r"diff --git a/(.*) b/(.*)")


def summarize_diff(text: str) -> list[FileChange]:
    """Per-file status and line counts from a unified diff (`git diff --no-color`)."""
    changes: list[FileChange] = []
    current: FileChange | None = None
    in_hunk = False
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            m = _HEADER.fullmatch(line)
            current = FileChange(path=m.group(2) if m else line[len("diff --git "):])
            if m and m.group(1) != m.group(2):
                current.old_path = m.group(1)
            changes.append(current)
            in_hunk = False
        elif current is None:
            continue
        elif not in_hunk and line.startswith("new file mode"):
            current.status, current.mode = "A", line[len("new file mode"):].strip()
        elif not in_hunk and line.startswith("deleted file mode"):
            current.status = "D"
        elif not in_hunk and line.startswith(("old mode ", "new mode ")):
            current.mode_changed = True
        elif not in_hunk and line.startswith(("Binary files ", "GIT binary patch")):
            current.binary = True
        elif not in_hunk and line.startswith("rename from "):
            current.status, current.old_path = "R", line[len("rename from "):]
        elif not in_hunk and line.startswith("rename to "):
            current.path = line[len("rename to "):]
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line.startswith("+"):
            current.added += 1
            current.added_lines.append(line[1:])
        elif in_hunk and line.startswith("-"):
            current.removed += 1
            current.removed_lines.append(line[1:])
    return changes


_ALLOWED = ("agents/", "src/codecheck/", "tests/", "skills/", "docs/")
_ALLOWED_FILES = {"README.md"}
# Nothing here may change in an unattended round: automation, hooks, the installer, instructions, this guard and
# everything it relies on, and the code that decides what an unattended run may do. Compared case-folded, so a path
# that only differs in case cannot slip past on a case-insensitive checkout.
_PROTECTED = (".github/", ".githooks/", ".claude/", "scripts/", "hooks/", "bin/", "installer/",
              "docs/decisions/", "docs/spikes/", "src/codecheck/providers/")
_PROTECTED_FILES = {
    "CLAUDE.md", "LICENSE",
    "src/codecheck/shadow/queue.py", "src/codecheck/commands/handoff_queue.py", "tests/test_handoff_queue.py",
    "src/codecheck/core/state.py", "src/codecheck/shadow/store.py", "src/codecheck/shadow/scrub.py",
    "src/codecheck/evals/golden.py", "src/codecheck/vcs/git.py", "src/codecheck/runids.py", "src/codecheck/errors.py",
    "src/codecheck/runner.py", "src/codecheck/cli.py", "src/codecheck/commands/__init__.py",
    "src/codecheck/config.py", "src/codecheck/commands/run_pr.py", "src/codecheck/commands/loop.py",
    "src/codecheck/core/policy.py", "src/codecheck/core/paths.py", "tests/__init__.py",
}
_PROTECTED_NAMES = {"conftest.py", "pytest.ini", "tox.ini", "setup.cfg"}  # anywhere: they can switch tests off
_SAFE_PATH = re.compile(r"[A-Za-z0-9_./+@=,~-]+")  # anything else (quotes, spaces, non-ASCII) fails closed
MAX_FILES = 60
# A release is a one-step patch bump of the version in these files and nothing else. The strict files may change
# only that way, on the file's own version line; the soft ones are ordinary source or tests that carry the version.
_SEMVER_TEXT = r"\d+\.\d+\.\d+"
_JSON_VERSION = rf'\s*"version": "{_SEMVER_TEXT}",?'
_VERSION_LINE = {"VERSION": _SEMVER_TEXT, "pyproject.toml": rf'version = "{_SEMVER_TEXT}"',
                 "package.json": _JSON_VERSION, "package-lock.json": _JSON_VERSION}
_VERSION_STRICT_DIRS = (".claude-plugin/",)
_VERSION_SOFT = {"src/codecheck/__init__.py", "tests/test_smoke.py"}
_SEMVER = re.compile(r"\b(\d+)\.(\d+)\.(\d+)\b")
_TEST_DEF = re.compile(r"\s*(async\s+)?def test_")
_SWITCHES_TESTS_OFF = re.compile(
    r"pytest\.mark\.(skip|skipif|xfail)|collect_ignore|pytest_sessionfinish|pytest_collection_modifyitems"
    r"|exitstatus|pytest\.skip\(|importorskip")


def _fold(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


_PROTECTED_FOLDED = tuple(_fold(p) for p in _PROTECTED)
_PROTECTED_FILES_FOLDED = {_fold(p) for p in _PROTECTED_FILES}


def _path_problem(path: str) -> str | None:
    parts = path.split("/")
    if not _SAFE_PATH.fullmatch(path) or path.startswith(("/", "-")) or ".." in parts or "" in parts:
        return "unsafe path"
    folded = _fold(path)
    if folded in _PROTECTED_FILES_FOLDED or folded.startswith(_PROTECTED_FOLDED) or parts[-1].casefold() in _PROTECTED_NAMES:
        return "protected path"
    return None


def _in_allowed_set(path: str) -> bool:
    return path in _ALLOWED_FILES or path.startswith(_ALLOWED)


def _is_strict_version_file(path: str) -> bool:
    return path in _VERSION_LINE or path.startswith(_VERSION_STRICT_DIRS)


def _is_patch_bump(c: FileChange) -> bool:
    """Every changed line is the same line before and after with only the version swapped, one patch step up."""
    if c.status != "M" or not c.added or c.added != c.removed:
        return False
    if _is_strict_version_file(c.path):
        shape = re.compile(_VERSION_LINE.get(c.path, _JSON_VERSION))
        if not all(shape.fullmatch(line) for line in c.added_lines + c.removed_lines):
            return False
    old = {m.group(0) for line in c.removed_lines for m in _SEMVER.finditer(line)}
    new = {m.group(0) for line in c.added_lines for m in _SEMVER.finditer(line)}
    if len(old) != 1 or len(new) != 1:
        return False
    o, n = (tuple(int(x) for x in next(iter(v)).split(".")) for v in (old, new))
    if n != (o[0], o[1], o[2] + 1):
        return False
    return all(_SEMVER.sub("V", a) == _SEMVER.sub("V", r) for a, r in zip(c.added_lines, c.removed_lines, strict=True))


def _file_violations(c: FileChange) -> list[str]:
    out = []
    for p in (c.path, c.old_path):
        if p is not None and (why := _path_problem(p)):
            out.append(f"{why}: {p}")
    if out:
        return out
    if c.binary:
        return [f"binary file not allowed: {c.path}"]
    if c.mode_changed or (c.mode is not None and c.mode != "100644"):
        return [f"file mode change or non-regular file not allowed: {c.path} ({c.mode or 'mode changed'})"]
    if c.path in _VERSION_SOFT or _is_strict_version_file(c.path):
        if _is_patch_bump(c):
            return out
        if _is_strict_version_file(c.path):
            return [f"{c.path} may only change by a one-step patch version bump"]
    if not _in_allowed_set(c.path):
        return [f"path outside the allowed set: {c.path}"]
    if c.status == "R":
        if c.old_path is None or not _in_allowed_set(c.old_path) or _is_strict_version_file(c.old_path):
            return [f"rename out of a path that may not change: {c.old_path}"]
        if c.old_path.startswith("tests/") or c.path.startswith("tests/"):
            return [f"renaming test files is not allowed: {c.old_path} -> {c.path}"]
    gone_from = _fold(c.old_path or c.path)
    if gone_from.startswith("tests/golden/") and c.status in ("M", "D", "R"):
        out.append(f"existing golden case may not change: {c.old_path or c.path}")
    if gone_from.startswith("tests/snippets/") and c.status == "D":
        out.append(f"snippet may not be deleted: {c.old_path or c.path}")
    if gone_from.startswith("tests/") and c.status == "D":
        out.append(f"test file removed: {c.old_path or c.path}")
    if c.path.startswith("tests/") and c.path.endswith(".py"):
        removed = sum(1 for line in c.removed_lines if _TEST_DEF.match(line))
        added = sum(1 for line in c.added_lines if _TEST_DEF.match(line))
        if removed > added:
            out.append(f"{c.path} removes {removed - added} test function(s)")
        if any(_SWITCHES_TESTS_OFF.search(line) for line in c.added_lines):
            out.append(f"{c.path} adds a line that can switch tests off (skip, xfail, collect_ignore, exit status)")
    return out


def violations(changes: list[FileChange]) -> list[str]:
    out = [v for c in changes for v in _file_violations(c)]
    total = sum(max(1, c.added + c.removed) for c in changes)  # a rename or a binary still counts
    if total > MAX_DIFF_LINES:
        out.append(f"diff changes {total} lines (limit {MAX_DIFF_LINES})")
    if len(changes) > MAX_FILES:
        out.append(f"diff touches {len(changes)} files (limit {MAX_FILES})")
    return out
