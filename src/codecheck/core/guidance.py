"""Safe reader for the repo's CODE_CHECK.md guidance (content goes into LLM prompts).

The root file applies to every file. A `CODE_CHECK.md` in a directory above a changed file is read too and applies only
to files under that directory; which ones are read follows from the changed paths alone, never from where the tool was
started, so PR, local and unattended runs agree. All of them share one `MAX_GUIDANCE_CHARS` budget, root first.
"""

import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from codecheck.vcs.git import Git

GUIDANCE_FILE = "CODE_CHECK.md"
MAX_GUIDANCE_CHARS = 16_000
MAX_GUIDANCE_DIRS = 500  # directories looked at for a nested file, shallowest first


@dataclass(frozen=True, slots=True)
class Guidance:
    source: str
    text: str
    truncated: bool
    files: tuple[str, ...] = (GUIDANCE_FILE,)  # repo-relative, in the order they appear in `text`

    def to_meta(self) -> dict:
        return {"source": self.source, "truncated": self.truncated, "files": list(self.files)}


def _decode(raw: bytes | str | None) -> str | None:
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return raw if raw and raw.strip() else None


def _rel(directory: str) -> str:
    return f"{directory}/{GUIDANCE_FILE}" if directory else GUIDANCE_FILE


def _plain(component: str) -> bool:
    return component not in ("", ".", "..") and component.isprintable() and "`" not in component


def guidance_dirs(paths: Iterable[object]) -> list[str]:
    """The directories above the given repo-relative files that may hold a nested file: shallowest first, then by name,
    without the root. A path that is absolute, climbs with `..` or holds control characters contributes nothing."""
    found: set[str] = set()
    for path in paths:
        if not isinstance(path, str):
            continue
        parts = path.split("/")[:-1]
        if not all(_plain(p) for p in parts):
            continue
        found.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    return sorted(found, key=lambda d: (d.count("/"), d))[:MAX_GUIDANCE_DIRS]


def _combine(source: str, sections: Iterable[tuple[str, bytes | str | None]]) -> Guidance | None:
    """One text from (directory, raw content) pairs given root first. A lone root file is passed through unchanged."""
    text, files, truncated = "", [], False
    for directory, raw in sections:
        body = _decode(raw)
        if body is None:
            continue
        if len(text) >= MAX_GUIDANCE_CHARS:
            truncated = True  # no room left for this file at all
            break
        if directory:
            if text and not text.endswith("\n"):
                text += "\n"
            gap = "\n" if text else ""
            # files come root first then shallowest first, so "above" is always a less specific file
            text += (f"{gap}[{_rel(directory)}: the rules below apply only to files under `{directory}/`; "
                     "for those files they win over any rule above that they conflict with]\n")
        text += body
        files.append(_rel(directory))
    if not files:
        return None
    if len(text) > MAX_GUIDANCE_CHARS:
        text = text[:MAX_GUIDANCE_CHARS]
        truncated = True
    if truncated:
        text += f"\n[{GUIDANCE_FILE} truncated at {MAX_GUIDANCE_CHARS} characters]"
    return Guidance(source, text, truncated, tuple(files))


def _read_regular(root: Path, directory: str) -> bytes | None:
    """The file in `directory` of the working tree, or None. Neither it nor any directory on the way may be a symlink."""
    path = root
    try:
        for part in directory.split("/") if directory else ():
            path = path / part
            if not stat.S_ISDIR(os.lstat(path).st_mode):
                return None
        path = path / GUIDANCE_FILE
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        # O_NOFOLLOW closes the lstat/open race for a swapped-in symlink.
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def from_worktree(root: Path | str, paths: Iterable[object] = ()) -> Guidance | None:
    """Guidance from the working tree for a change to `paths`: the root file, then nested ones above those paths."""
    root = Path(root)
    return _combine("worktree", ((d, _read_regular(root, d)) for d in ("", *guidance_dirs(paths))))


def from_base(git: Git, ref: str, paths: Iterable[object] = ()) -> Guidance | None:
    """Guidance at `ref` for a change to `paths`: the root file, then nested ones above those paths."""
    dirs = guidance_dirs(paths)
    nested = set(git.regular_files(ref, [_rel(d) for d in dirs])) if dirs else set()
    return _combine("base", ((d, git.show_file(ref, _rel(d))) for d in ("", *dirs) if not d or _rel(d) in nested))
