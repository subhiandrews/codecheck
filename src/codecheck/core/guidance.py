"""Safe reader for the repo's CODE_CHECK.md guidance (content goes into LLM prompts)."""

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from codecheck.vcs.git import Git

GUIDANCE_FILE = "CODE_CHECK.md"
MAX_GUIDANCE_CHARS = 16_000


@dataclass(frozen=True, slots=True)
class Guidance:
    source: str
    text: str
    truncated: bool

    def to_meta(self) -> dict:
        return {"source": self.source, "truncated": self.truncated}


def _normalise(source: str, raw: bytes | str | None) -> Guidance | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not raw.strip():
        return None
    if len(raw) > MAX_GUIDANCE_CHARS:
        text = raw[:MAX_GUIDANCE_CHARS] + f"\n[{GUIDANCE_FILE} truncated at {MAX_GUIDANCE_CHARS} characters]"
        return Guidance(source, text, True)
    return Guidance(source, raw, False)


def from_worktree(root: Path | str) -> Guidance | None:
    path = Path(root) / GUIDANCE_FILE
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        # O_NOFOLLOW closes the lstat/open race for a swapped-in symlink.
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    return _normalise("worktree", raw)


def from_base(git: Git, ref: str) -> Guidance | None:
    return _normalise("base", git.show_file(ref, GUIDANCE_FILE))
