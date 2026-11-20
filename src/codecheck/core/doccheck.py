"""Documentation measures for the docs reviewer: comment ratio of the added lines and new doc files (no LLM).

Comments are found with a line scanner, not a parser, and diffs show fragments, so the scan restarts at every
gap between hunks. It is a measure to hand a reviewer, not a verdict.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from codecheck.core.diffmap import FileDiff

DOC_SUFFIXES = (".md", ".mdx", ".rst", ".adoc", ".txt")
DOC_STEMS = ("README", "CHANGELOG", "CHANGES", "HISTORY", "CONTRIBUTING", "NOTES", "AUTHORS")
_HASH = {"python", "ruby", "shell"}
_SLASH = {"javascript", "typescript", "java", "csharp", "cpp", "c", "go", "rust", "php", "swift", "kotlin"}
_PY_DOC = re.compile(r"""^[rRuUbBfF]{0,2}(\"\"\"|''')""")


def is_doc_path(path: str) -> bool:
    p = PurePosixPath(path)
    return p.suffix.lower() in DOC_SUFFIXES or (not p.suffix and p.name.upper() in DOC_STEMS)


def _step(language: str, s: str) -> tuple[bool, str]:
    """Classify one stripped, non-blank line. Returns (is_comment, state to carry into the next line)."""
    if language == "python":
        m = _PY_DOC.match(s)
        if m:
            q = m.group(1)
            return True, "" if s.count(q) >= 2 else q
    if (language in _HASH or language == "php") and s.startswith("#") and not s.startswith("#!"):
        return True, ""
    if language == "sql" or language in _SLASH:
        if s.startswith("/*"):
            return True, "" if "*/" in s[2:] else "*/"
        if s.startswith("--" if language == "sql" else "//"):
            return True, ""
    return False, ""


def comment_flags(language: str, lines: Sequence[str]) -> list[bool | None]:
    """True for a comment or docstring line, False for code, None for a blank line."""
    out: list[bool | None] = []
    state = ""  # "*/" inside a block comment, a triple quote inside a docstring
    for raw in lines:
        s = raw.strip()
        if not s:
            out.append(None)
        elif state:
            out.append(True)
            if state in s:
                state = ""
        else:
            is_comment, state = _step(language, s)
            out.append(is_comment)
    return out


@dataclass(frozen=True, slots=True)
class DocMetrics:
    comment_lines: int
    code_lines: int
    cap_percent: int
    doc_files: tuple[dict, ...]
    files: tuple[dict, ...]

    @property
    def percent(self) -> float:
        total = self.comment_lines + self.code_lines
        return round(100 * self.comment_lines / total, 1) if total else 0.0

    @property
    def over_cap(self) -> bool:
        return self.percent > self.cap_percent

    def to_json(self) -> dict:
        return {"cap_percent": self.cap_percent, "comment_lines_added": self.comment_lines,
                "code_lines_added": self.code_lines, "comment_percent": self.percent, "over_cap": self.over_cap,
                "doc_files": list(self.doc_files), "files_with_comments": list(self.files)}


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


def measure(diffs: Mapping[str, FileDiff], languages: Mapping[str, str | None], cap_percent: int) -> DocMetrics:
    comments = code = 0
    per_file, doc_files = [], []
    for path, fd in diffs.items():
        if fd.binary:
            continue
        added = sum(1 for ln in fd.lines if ln.kind == "add")
        if is_doc_path(path) and added and fd.status != "deleted":
            doc_files.append({"path": path, "status": fd.status, "added_lines": added})
        language = languages.get(path)
        if not language:
            continue
        c = k = 0
        for run in runs(fd):
            for ln, flag in zip(run, comment_flags(language, [x.text for x in run]), strict=True):
                if ln.kind == "add" and flag is not None:
                    c, k = (c + 1, k) if flag else (c, k + 1)
        comments, code = comments + c, code + k
        if c:
            per_file.append({"path": path, "comment_lines": c, "code_lines": k})
    per_file.sort(key=lambda f: -f["comment_lines"])
    return DocMetrics(comments, code, cap_percent, tuple(doc_files), tuple(per_file[:10]))
