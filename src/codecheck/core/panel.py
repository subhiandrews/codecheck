"""Language detection, ignore globs and reviewer-panel selection (no LLM)."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

LANGUAGES: dict[str, dict[str, tuple[str, ...]]] = {
    "python": {"ext": (".py", ".pyi"), "shebang": ("python",)},
    "javascript": {"ext": (".js", ".jsx", ".mjs", ".cjs"), "shebang": ("node",)},
    "typescript": {"ext": (".ts", ".tsx", ".mts", ".cts"), "shebang": ("ts-node", "tsx", "deno")},
    "java": {"ext": (".java",), "shebang": ()},
    "csharp": {"ext": (".cs",), "shebang": ()},
    "cpp": {"ext": (".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx"), "shebang": ()},
    "c": {"ext": (".c", ".h"), "shebang": ()},
    "go": {"ext": (".go",), "shebang": ()},
    "rust": {"ext": (".rs",), "shebang": ()},
    "php": {"ext": (".php",), "shebang": ("php",)},
    "ruby": {"ext": (".rb",), "shebang": ("ruby",)},
    "swift": {"ext": (".swift",), "shebang": ("swift",)},
    "kotlin": {"ext": (".kt", ".kts"), "shebang": ()},
    "sql": {"ext": (".sql",), "shebang": ()},
    "shell": {"ext": (".sh", ".bash", ".zsh"), "shebang": ("sh", "bash", "zsh", "dash")},
}
CORE_REVIEWERS = ("architect", "pragmatist", "skeptic")


def detect_language(path: str, first_line: str = "") -> str | None:
    suffix = PurePosixPath(path).suffix.lower()
    for name, spec in LANGUAGES.items():
        if suffix and suffix in spec["ext"]:
            return name
    if first_line.startswith("#!"):
        tokens = [re.sub(r"[\d.]+$", "", t) for t in first_line[2:].replace("/", " ").split()]
        for name, spec in LANGUAGES.items():
            if any(t in spec["shebang"] for t in tokens):
                return name
    return None


def group_by_language(paths: Sequence[str], first_lines: Mapping[str, str] | None = None) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for p in paths:
        lang = detect_language(p, (first_lines or {}).get(p, ""))
        if lang:
            groups.setdefault(lang, []).append(p)
    return groups


def is_ignored(path: str, patterns: Sequence[str]) -> bool:
    p = PurePosixPath(path)
    return any(p.full_match(pat) for pat in patterns)


def filter_ignored(paths: Sequence[str], patterns: Sequence[str]) -> list[str]:
    return [p for p in paths if not is_ignored(p, patterns)]


@dataclass(frozen=True, slots=True)
class Panel:
    core: tuple[str, ...]
    languages: tuple[str, ...]
    deep: bool
    notes: tuple[str, ...] = ()
    security: bool = False

    @property
    def members(self) -> tuple[str, ...]:
        return (*self.core, *(("security",) if self.security else ()), *(f"lang-{lang}" for lang in self.languages), *(("deep-reviewer",) if self.deep else ()))


def select_panel(
    paths: Sequence[str],
    first_lines: Mapping[str, str] | None = None,
    *,
    deep_enabled: bool = False,
    deep_available: bool = False,
    security: bool = False,
) -> Panel:
    languages = tuple(sorted(group_by_language(paths, first_lines)))
    notes: tuple[str, ...] = ()
    if deep_enabled and not deep_available:
        notes = ("deep-reviewer: enabled but unavailable, skipped",)
    return Panel(CORE_REVIEWERS, languages, deep_enabled and deep_available, notes, security)
