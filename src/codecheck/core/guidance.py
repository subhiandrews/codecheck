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

from codecheck.errors import GitError
from codecheck.vcs.git import Git

GUIDANCE_FILE = "CODE_CHECK.md"
CONTEXT_FILE = "CLAUDE.md"  # the repo's own instructions to coding agents; read the same safe way, as background only
MAX_GUIDANCE_CHARS = 16_000
NESTED_RESERVE = 4_000  # of the cap, kept for nested files when there are any
MIN_NESTED_BODY = 200  # a nested file is cut only when at least this much of it fits
MAX_GUIDANCE_DIRS = 500  # directories looked at for a nested file, shallowest first


@dataclass(frozen=True, slots=True)
class Guidance:
    source: str
    text: str
    truncated: bool
    files: tuple[str, ...] = (GUIDANCE_FILE,)  # repo-relative, in the order they appear in `text`
    skipped: tuple[str, ...] = ()  # nested files that found no room under the cap and are not in `text`

    def to_meta(self) -> dict:
        return {"source": self.source, "truncated": self.truncated, "files": list(self.files), "skipped": list(self.skipped)}


def _decode(raw: bytes | str | None) -> str | None:
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return raw if raw and raw.strip() else None


def _rel(directory: str, name: str = GUIDANCE_FILE) -> str:
    return f"{directory}/{name}" if directory else name


def _plain(component: str) -> bool:
    return component not in ("", ".", "..") and component.isprintable() and "`" not in component


def _ancestor_dirs(paths: Iterable[object]) -> list[str]:
    found: set[str] = set()
    for path in paths:
        if not isinstance(path, str):
            continue
        parts = path.split("/")[:-1]
        if not all(_plain(p) for p in parts):
            continue
        found.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    return sorted(found, key=lambda d: (d.count("/"), d))


def guidance_dirs(paths: Iterable[object], name: str = GUIDANCE_FILE) -> tuple[list[str], tuple[str, ...]]:
    """The directories above the given repo-relative files that may hold a nested file, and a note for the guidance text
    when `MAX_GUIDANCE_DIRS` cut some off. Shallowest first, then by name, without the root. A path that is absolute,
    climbs with `..` or holds control characters contributes nothing."""
    found = _ancestor_dirs(paths)
    note = (f"[{name}: only the {MAX_GUIDANCE_DIRS} shallowest directories above the changed files were "
            "checked for nested files]")
    return found[:MAX_GUIDANCE_DIRS], (note,) if len(found) > MAX_GUIDANCE_DIRS else ()


def _header(directory: str, name: str = GUIDANCE_FILE) -> str:
    # files come root first then shallowest first, so "above" is always a less specific file
    return (f"[{_rel(directory, name)}: the rules below apply only to files under `{directory}/`; "
            "for those files they win over any rule above that they conflict with]\n")


def _combine(source: str, sections: Iterable[tuple[str, bytes | str | None]], notes: tuple[str, ...] = (),
             name: str = GUIDANCE_FILE) -> Guidance | None:
    """One text from (directory, raw content) pairs given root first. A lone root file is passed through unchanged.

    Nested files keep up to `NESTED_RESERVE` characters of the cap (or what they need, if less) so a large root file
    cannot crowd out the most specific rules. A nested part is added whole, or cut only when it keeps at least
    `MIN_NESTED_BODY` characters of its body; otherwise it is left out and named in `skipped`, never half-listed."""
    parts = [(d, body) for d, raw in sections if (body := _decode(raw)) is not None]
    if not parts:
        return None
    nested_size = sum(len(_header(d, name)) + len(body) + 2 for d, body in parts if d)
    root_cap = MAX_GUIDANCE_CHARS - min(NESTED_RESERVE, nested_size)
    text, files, skipped, truncated = "", [], [], False
    for directory, body in parts:
        sep = "" if not text else ("" if text.endswith("\n") else "\n") + "\n"
        head = _header(directory, name) if directory else ""
        room = (MAX_GUIDANCE_CHARS if directory else root_cap) - len(text) - len(sep)
        if len(head) + len(body) <= room:
            text += sep + head + body
        elif room >= len(head) + (MIN_NESTED_BODY if directory else 0):
            text += sep + (head + body)[:room]
            truncated = True
        else:
            skipped.append(_rel(directory, name))
            truncated = True
            continue
        files.append(_rel(directory, name))
    if truncated:
        text += f"\n[{name} truncated at {MAX_GUIDANCE_CHARS} characters]"
    for note in notes:  # a lookup that could not be completed: say so rather than look complete
        text += f"\n{note}"
        truncated = True
    return Guidance(source, text, truncated, tuple(files), tuple(skipped))


def read_regular(root: Path, directory: str, name: str = GUIDANCE_FILE) -> bytes | None:
    """The file in `directory` of the working tree, or None. Neither it nor any directory on the way may be a symlink."""
    path = root
    try:
        for part in directory.split("/") if directory else ():
            path = path / part
            if not stat.S_ISDIR(os.lstat(path).st_mode):
                return None
        path = path / name
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        # O_NOFOLLOW closes the lstat/open race for a swapped-in symlink.
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def from_worktree(root: Path | str, paths: Iterable[object] = (), name: str = GUIDANCE_FILE) -> Guidance | None:
    """Guidance from the working tree for a change to `paths`: the root file, then nested ones above those paths."""
    root = Path(root)
    dirs, notes = guidance_dirs(paths, name)
    return _combine("worktree", ((d, read_regular(root, d, name)) for d in ("", *dirs)), notes, name)


def from_base(git: Git, ref: str, paths: Iterable[object] = (), notes: tuple[str, ...] = (),
              name: str = GUIDANCE_FILE) -> Guidance | None:
    """Guidance at `ref` for a change to `paths`: the root file, then nested ones above those paths. `notes` are lookup
    problems the caller already knows of; they are shown in the text and mark it truncated."""
    dirs, capped = guidance_dirs(paths, name)
    notes += capped
    nested: set[str] = set()
    if dirs:
        try:
            nested = set(git.regular_files(ref, [_rel(d, name) for d in dirs]))
        except GitError:
            notes += (f"[{name}: nested files could not be looked up]",)  # the root file still applies
    return _combine("base", ((d, git.show_file(ref, _rel(d, name))) for d in ("", *dirs) if not d or _rel(d, name) in nested),
                    notes, name)
