"""Synthetic cases: keep the lesson of a private or non-permissive PR and drop its code.

`build_brief` turns a labelled case into what is general about it (language, category, severity, whether the finding
was right) and nothing that identifies it. `check_synthetic` verifies a hand-written live snippet against the source:
it must share nothing distinctive with it and must match the brief. Both are deterministic; the model that writes the
snippet never sees the source (see docs/superpowers/specs/2026-11-19-synthetic-cases-design.md).
"""

import json
import re
from collections import Counter
from collections.abc import Collection, Mapping

from codecheck.errors import CodeCheckError

BRIEF_VERSION = 1
MIN_LINE = 30  # a whole line this long that appears in both is a copy, not a coincidence
MIN_IDENTIFIER = 12  # a plain word this long is distinctive enough to count
_LANGUAGES = {
    ".py": "python", ".go": "go", ".rs": "rust", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".java": "java", ".kt": "kotlin", ".rb": "ruby", ".php": "php",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp", ".swift": "swift",
    ".sh": "shell", ".sql": "sql",
}
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL = re.compile(r"[a-z][A-Z]|[A-Z]{2,}[a-z]")
_PATH = re.compile(r"[\w.-]+(?:/[\w.-]+)+")
_HOST = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_HOST_TLDS = {"com", "org", "net", "io", "dev", "internal", "local", "corp", "example", "test", "lan", "intranet"}
_INSTRUCTIONS = (
    "Write an invented project in this language with one small change that has the defect each pattern describes, "
    "as a live snippet: base files, change files, the findings to expect and the reports that must not appear. "
    "Use a fresh project name, file names and identifiers. Never reuse anything from real code."
)


def _language(paths: list[str]) -> str:
    counts = Counter(_LANGUAGES.get("." + p.rsplit(".", 1)[-1].lower()) for p in paths if "." in p.rsplit("/", 1)[-1])
    counts.pop(None, None)
    return counts.most_common(1)[0][0] if counts else "unknown"


def _labelled(case: Mapping) -> list[dict]:
    return [lab for lab in case.get("labels") or [] if isinstance(lab, dict) and lab.get("label") in ("expected", "not_expected")]


def build_brief(case: Mapping) -> dict:
    """What is general about a labelled case. Holds no file name, line, message, code, repository, PR or id."""
    labelled = _labelled(case)
    if not labelled:
        raise CodeCheckError("the case has no labelled findings (expected or not_expected): nothing to learn from")
    return {
        "version": BRIEF_VERSION, "kind": "synthetic-brief",
        "language": _language([str(lab.get("file") or "") for lab in labelled]),
        "findings": [{"label": lab["label"], "category": str(lab.get("category") or ""), "severity": str(lab.get("severity") or ""),
                      "pattern": ""} for lab in labelled],
        "instructions": _INSTRUCTIONS,
    }


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def _distinctive(token: str) -> bool:
    if token.startswith("__") and token.endswith("__"):
        return False  # python dunders are language, not project
    return "_" in token.strip("_") or bool(_CAMEL.search(token)) or len(token) >= MIN_IDENTIFIER


def _host(token: str) -> bool:
    parts = token.lower().split(".")
    return len(parts) >= 3 or parts[-1] in _HOST_TLDS


def _normal(line: str) -> str:
    return " ".join(line.lstrip("+- ").split()).lower()


def _source_text(case: Mapping) -> str:
    texts = [str(case.get("diff") or ""), str(case.get("repo") or "")]
    texts += _strings(case.get("reviewer_outputs")) + _strings(case.get("aggregator")) + _strings(case.get("expected_posted"))
    texts += [str(lab.get("file") or "") for lab in case.get("labels") or [] if isinstance(lab, dict)]
    return "\n".join(texts)


def leak_terms(case: Mapping) -> set[str]:
    """Lowercase terms that would identify the source: compound identifiers, long words, paths, host names, owner."""
    text = _source_text(case)
    terms = {t.lower() for t in _IDENT.findall(text) if _distinctive(t)}
    terms |= {p.lower() for p in _PATH.findall(text)}
    terms |= {h.lower() for h in _HOST.findall(text) if _host(h)}
    repo = str(case.get("repo") or "")
    if "/" in repo:
        owner = repo.split("/", 1)[0].lower()
        terms |= {repo.lower()} | ({owner} if len(owner) >= 4 else set())
    return {t for t in terms if len(t) >= 4}


def _long_lines(texts: list[str]) -> set[str]:
    return {n for t in texts for line in t.splitlines() if len(n := _normal(line)) >= MIN_LINE}


def check_synthetic(snippet: Mapping, source: Mapping, *, brief: Mapping | None = None, allow: Collection[str] = ()) -> list[str]:
    """Why this snippet may not stand in for the source, or [] when it may."""
    from codecheck.evals.live import (
        _problems as snippet_problems,  # lazy: evals.live imports the commands package
    )

    schema = snippet_problems(dict(snippet))
    if schema:
        return schema
    problems = []
    if snippet.get("calibrated") is True:
        problems.append("calibrated must be absent or false: run `eval --live --only <name>` twice before setting it")
    files = {**snippet.get("base", {}), **snippet.get("change", {})}
    body = "\n".join([str(snippet.get("name", "")), *files, *files.values()])
    lowered, allowed = body.lower(), {a.strip().lower() for a in allow}
    problems += [f"shares {t!r} with the source (use another name, or --allow it if it is generic)"
                 for t in sorted(leak_terms(source) - allowed) if t in lowered]
    copied = _long_lines(list(files.values())) & _long_lines([str(source.get("diff") or "")])
    problems += [f"copies a line from the source: {line[:60]!r}" for line in sorted(copied)]
    if brief is not None:
        problems += _against_brief(snippet, brief)
    return problems


def _against_brief(snippet: Mapping, brief: Mapping) -> list[str]:
    findings = [f for f in brief.get("findings") or [] if isinstance(f, dict)]
    want = Counter(f.get("severity") for f in findings if f.get("label") == "expected")
    got = Counter(e.get("severity") for e in snippet.get("expected") or [] if isinstance(e, dict))
    out = []
    if sum(want.values()) != sum(got.values()):
        out.append(f"the brief expects {sum(want.values())} expected finding(s); the snippet has {sum(got.values())}")
    elif want != got:
        out.append(f"the severities differ from the brief: want {dict(want)}, got {dict(got)}")
    wrong = sum(1 for f in findings if f.get("label") == "not_expected")
    if len(snippet.get("must_not_report") or []) < wrong:
        out.append(f"the brief has {wrong} finding(s) the reviewer must not report; must_not_report has "
                   f"{len(snippet.get('must_not_report') or [])}")
    return out


def load_json_object(path, what: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CodeCheckError(f"{what} {path}: not readable JSON: {e}") from e
    if not isinstance(data, dict):
        raise CodeCheckError(f"{what} {path}: expected a JSON object")
    return data
