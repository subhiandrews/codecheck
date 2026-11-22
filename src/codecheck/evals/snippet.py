"""The live-eval snippet schema. Pure (no I/O, no commands), so anything can validate a snippet without importing the
review machinery."""

import re
from pathlib import PurePosixPath

SEVERITIES = ("critical", "warning", "suggestion")


# Code points HFS+ ignores in names, so `.g\u200cit` is `.git` there (git's own core.protectHFS list).
_HFS_IGNORED = re.compile("[\u200c-\u200f\u202a-\u202e\u206a-\u206f\ufeff]")


def _is_git_dir_name(part: str) -> bool:
    """`.git` however a filesystem may spell it: any case (APFS and NTFS default to case-insensitive), with
    HFS-ignored characters or trailing dots and spaces (NTFS), or as an 8.3 short name like `GIT~1`."""
    name = _HFS_IGNORED.sub("", part).lower().rstrip(". ")
    return name == ".git" or re.fullmatch(r"git~\d+", name) is not None


def path_ok(rel: object) -> bool:
    if not isinstance(rel, str) or not rel or "\0" in rel or "\\" in rel:
        return False
    p = PurePosixPath(rel)
    return not p.is_absolute() and all(part != ".." and not _is_git_dir_name(part) for part in p.parts)


def path_glob_ok(value: object) -> bool:
    return isinstance(value, str) and (value == "*" or (value.endswith("/**") and path_ok(value[:-3])))


def _category_ok(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return isinstance(value, list) and bool(value) and all(isinstance(v, str) and v.strip() for v in value)


def _severities_ok(value: object) -> bool:
    return isinstance(value, list) and bool(value) and all(v in SEVERITIES for v in value)


def _int_line(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _memory_effect_problems(snippet: dict) -> list[str]:
    """`memory_effect: {off: {expected?, must_not_report?}}`: the memory-off arm's own expectations for `eval --memory-effect`."""
    effect = snippet["memory_effect"]
    if not snippet.get("memory"):
        return ["memory_effect needs memory lessons"]
    off = effect.get("off") if isinstance(effect, dict) else None
    if not (isinstance(effect, dict) and set(effect) <= {"off"} and isinstance(off, dict) and set(off) <= {"expected", "must_not_report"}):
        return ["memory_effect must be {off: {expected?, must_not_report?}}"]
    arm = {"name": "x", "base": {}, "change": {"x": ""}, "expected": snippet.get("expected", []),
           "must_not_report": snippet.get("must_not_report", []), **off}
    return [f"memory_effect.off: {p}" for p in snippet_problems(arm)]


def snippet_problems(snippet: object) -> list[str]:
    if not isinstance(snippet, dict):
        return ["expected a JSON object"]
    out = []
    if not isinstance(snippet.get("name"), str) or not snippet["name"].strip():
        out.append("name must be a non-empty string")
    for key in ("base", "change"):
        files = snippet.get(key)
        if not isinstance(files, dict):
            out.append(f"{key} must be an object of path: content")
            continue
        for rel, text in files.items():
            if not path_ok(rel):
                out.append(f"{key}: path {rel!r} must be relative, without '..' or '.git' (it would escape the repository)")
            if not isinstance(text, str):
                out.append(f"{key}: content of {rel!r} must be a string")
    if not snippet.get("change"):
        out.append("change must not be empty: there would be nothing to review")
    expected = snippet.get("expected")
    if not isinstance(expected, list):
        out.append("expected must be a list")
    else:
        for i, e in enumerate(expected):
            if not (isinstance(e, dict) and path_ok(e.get("file")) and _int_line(e.get("line"))
                    and _category_ok(e.get("category")) and e.get("severity") in SEVERITIES):
                out.append(f"expected[{i}] needs file, line (>= 1), category (a name or a list of names) and "
                           f"severity ({', '.join(SEVERITIES)})")
    rules = snippet.get("must_not_report", [])
    if not isinstance(rules, list):
        out.append("must_not_report must be a list")
    else:
        for i, r in enumerate(rules):
            if not (isinstance(r, dict) and path_ok(r.get("file"))
                    and (r.get("category") is None or _category_ok(r["category"]))
                    and (r.get("line") is None or _int_line(r["line"]))
                    and (r.get("severity") is None or _severities_ok(r["severity"]))):
                out.append(f"must_not_report[{i}] needs file, and optionally category, line and severity "
                           f"(a non-empty list of {', '.join(SEVERITIES)})")
    memory = snippet.get("memory")
    if memory is not None:
        lessons = memory.get("lessons") if isinstance(memory, dict) else None
        if not (isinstance(lessons, list) and lessons and all(
                isinstance(le, dict) and le.get("kind") in ("miss", "finding") and path_glob_ok(le.get("path_glob"))
                and isinstance(le.get("statement"), str) and le["statement"].strip()
                and all(isinstance(le.get(k, ""), str) for k in ("language", "category", "lean"))
                for le in lessons)):
            out.append("memory must be {lessons: [{kind, path_glob, statement, language?, category?, lean?}]}")
    if "memory_effect" in snippet:
        out += _memory_effect_problems(snippet)
    if "calibrated" in snippet and not isinstance(snippet["calibrated"], bool):
        out.append("calibrated must be true or false")
    if "live" in snippet and not isinstance(snippet["live"], bool):
        out.append("live must be true or false")
    return out
