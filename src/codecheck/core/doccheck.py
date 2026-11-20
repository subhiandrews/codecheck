"""Documentation measures for the docs reviewer: comment ratio of the added lines and new doc files (no LLM).

Comments are found with a line scanner, not a parser, and diffs show fragments, so the scan restarts at every
gap between hunks. It is a measure to hand a reviewer, not a verdict.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from codecheck.core.diffmap import DiffLine, FileDiff

DOC_SUFFIXES = (".md", ".mdx", ".rst", ".adoc")
DOC_STEMS = ("README", "CHANGELOG", "CHANGES", "HISTORY", "CONTRIBUTING", "NOTES", "AUTHORS")
_HASH = {"python", "ruby", "shell"}
_SLASH = {"javascript", "typescript", "java", "csharp", "cpp", "c", "go", "rust", "php", "swift", "kotlin"}
_PY_DOC = re.compile(r"""^[rRuUbBfF]{0,2}(\"\"\"|''')""")
_TRIPLE = re.compile(r"\"\"\"|'''")
_QUOTES = ('"""', "'''")

Scan = dict[str, list[list[tuple[DiffLine, bool | None]]]]  # path -> runs of (line, comment flag)


def is_doc_path(path: str) -> bool:
    p = PurePosixPath(path)
    return p.suffix.lower() in DOC_SUFFIXES or (not p.suffix and p.name.upper() in DOC_STEMS)


def _step(language: str, s: str) -> tuple[bool, str]:
    """Classify one stripped, non-blank line of a non-Python language. Returns (is_comment, state for the next line)."""
    if (language in _HASH or language == "php") and s.startswith("#") and not s.startswith("#!"):
        return True, ""
    if language == "sql" or language in _SLASH:
        if s.startswith("/*"):
            return True, "" if "*/" in s[2:] else "*/"
        if s.startswith("--" if language == "sql" else "//"):
            return True, ""
    return False, ""


def _python_flags(lines: Sequence[str], mid: bool) -> list[bool | None]:
    """A string that opens at the start of a line is a docstring (comment); one opened after code is code."""
    out: list[bool | None] = []
    state: tuple[str, bool] | None = None  # (quote, is a docstring)
    if mid:
        # Triple quotes pair up, so an odd count of one kind means the first closes a string opened before this run.
        firsts = [min(i for i, x in enumerate(lines) if q in x) for q in _QUOTES if sum(x.count(q) for x in lines) % 2]
        if firsts:
            state = (next(q for q in _QUOTES if q in lines[min(firsts)]), True)
    for raw in lines:
        s = raw.strip()
        if not s:
            out.append(None)
        elif state:
            out.append(state[1])
            if state[0] in s:
                state = None
        elif s.startswith("#") and not s.startswith("#!"):
            out.append(True)
        elif m := _PY_DOC.match(s):
            out.append(True)
            if s.count(m.group(1)) < 2:
                state = (m.group(1), True)
        else:
            if (t := _TRIPLE.search(s)) and s.count(t.group(0)) % 2:
                state = (t.group(0), False)
            out.append(False)
    return out


def comment_flags(language: str, lines: Sequence[str], mid: bool = False) -> list[bool | None]:
    """True for a comment or docstring line, False for code, None for a blank line.

    `mid` means the lines start part-way through a file, so a comment may already be open at the first line.
    """
    if language == "python":
        return _python_flags(lines, mid)
    out: list[bool | None] = []
    state = ""  # "*/" inside a block comment
    first = True
    for raw in lines:
        s = raw.strip()
        if not s:
            out.append(None)
            continue
        if first and mid and (language in _SLASH or language == "sql") and s.startswith("*/"):
            out.append(True)
        elif state:
            out.append(True)
            if state in s:
                state = ""
        else:
            is_comment, state = _step(language, s)
            out.append(is_comment)
        first = False
    return out


@dataclass(frozen=True, slots=True)
class DocMetrics:
    comment_lines: int
    code_lines: int
    cap_percent: int
    doc_files: tuple[dict, ...]
    files: tuple[dict, ...]
    min_comment_lines: int = 0  # fewer comment lines than this never count as over the cap

    @property
    def percent(self) -> float:
        total = self.comment_lines + self.code_lines
        return round(100 * self.comment_lines / total, 1) if total else 0.0

    @property
    def over_cap(self) -> bool:
        return (self.comment_lines >= max(1, self.min_comment_lines)
                and 100 * self.comment_lines > self.cap_percent * (self.comment_lines + self.code_lines))

    def to_json(self) -> dict:
        return {"cap_percent": self.cap_percent, "comment_lines_added": self.comment_lines,
                "code_lines_added": self.code_lines, "comment_percent": self.percent, "over_cap": self.over_cap,
                "min_comment_lines": self.min_comment_lines,
                "doc_files": list(self.doc_files), "files_with_comments": list(self.files)}


def starts_mid(run: Sequence[DiffLine]) -> bool:
    return bool(run) and run[0].new is not None and run[0].new > 1


def runs(fd: FileDiff):
    """Contiguous runs of right-side lines (added and context), so a gap between hunks resets the scan."""
    run, prev = [], None
    for ln in fd.lines:
        if ln.new is None:
            continue
        if prev is not None and ln.new != prev + 1:
            yield run
            run = []
        run.append(ln)
        prev = ln.new
    if run:
        yield run


def scan(diffs: Mapping[str, FileDiff], languages: Mapping[str, str | None]) -> Scan:
    """Flag every right-side line of every text file that has a known language, once, for all consumers."""
    out: Scan = {}
    for path, fd in diffs.items():
        language = languages.get(path)
        if fd.binary or not language:
            continue
        out[path] = [list(zip(run, comment_flags(language, [x.text for x in run], mid=starts_mid(run)), strict=True))
                     for run in runs(fd)]
    return out


def measure(diffs: Mapping[str, FileDiff], languages: Mapping[str, str | None], cap_percent: int,
            min_comment_lines: int = 0, scans: Scan | None = None) -> DocMetrics:
    scans = scan(diffs, languages) if scans is None else scans
    comments = code = 0
    per_file, doc_files = [], []
    for path, fd in diffs.items():
        if fd.binary:
            continue
        added = sum(1 for ln in fd.lines if ln.kind == "add")
        if is_doc_path(path) and added and fd.status != "deleted":
            doc_files.append({"path": path, "status": fd.status, "added_lines": added})
        c = k = 0
        for run in scans.get(path, ()):
            for ln, flag in run:
                if ln.kind == "add" and flag is not None:
                    c, k = (c + 1, k) if flag else (c, k + 1)
        comments, code = comments + c, code + k
        if c:
            per_file.append({"path": path, "comment_lines": c, "code_lines": k})
    per_file.sort(key=lambda f: -f["comment_lines"])
    return DocMetrics(comments, code, cap_percent, tuple(doc_files), tuple(per_file[:10]), min_comment_lines)
