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

from codecheck.core.panel import detect_language
from codecheck.errors import CodeCheckError
from codecheck.evals.snippet import snippet_problems

BRIEF_VERSION = 1
MIN_LINE = 30  # a whole line this long that appears in both is a copy, not a coincidence
MIN_IDENTIFIER = 12  # a plain word this long, in code, is distinctive enough to count
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL = re.compile(r"[a-z][A-Z]|[A-Z]{2,}[a-z]")
_BACKTICKED = re.compile(r"`([^`\n]+)`")
_PATH = re.compile(r"[\w.-]+(?:/[\w.-]+)+")
_HOST = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_HOST_TLDS = {"com", "org", "net", "io", "dev", "internal", "local", "corp", "example", "test", "lan", "intranet"}
_INSTRUCTIONS = (
    "Write an invented project in this language with one small change that has the defect each pattern describes, "
    "as a live snippet: base files, change files, the findings to expect and the reports that must not appear. "
    "Use a fresh project name, file names and identifiers. Never reuse anything from real code. "
    "Point each expected finding at the line a reviewer would anchor on: the statement or call that is wrong, not a handler "
    "beside it (a match must land within 2 lines). The severities here come from a larger project, so choose the severity the "
    "defect has when your snippet stands alone; an unexpected Critical fails the run."
)


def _language(paths: list[str]) -> str:
    counts = Counter(lang for p in paths if (lang := detect_language(p)))
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
        "allow": [],  # generic API names (standard library, well-known crates) the defect needs; a person fills this in
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


def _distinctive(token: str, *, long_words: bool = True) -> bool:
    """A compound identifier, or (in code only) a long plain word. Python dunders are language, not project."""
    if token.startswith("__") and token.endswith("__"):
        return False
    return "_" in token.strip("_") or bool(_CAMEL.search(token)) or (long_words and len(token) >= MIN_IDENTIFIER)


def _is_host(token: str) -> bool:
    """A name with a real top-level domain: three or more labels, or two under com, org or net. `os.path.join` and
    `cfg.dev` are attribute chains, not hosts."""
    parts = token.lower().split(".")
    if parts[-1] not in _HOST_TLDS:
        return False
    return len(parts) >= 3 or parts[-1] in {"com", "org", "net"}


def _is_path(token: str) -> bool:
    """`a/b.py` and `a/b/c` are paths; `and/or` and `read/write` are prose."""
    return token.count("/") >= 2 or "." in token.rsplit("/", 1)[-1]


def _normal(line: str) -> str:
    return " ".join(line.lstrip("+- ").split()).lower()


def _code_terms(text: str, *, long_words: bool = True) -> set[str]:
    return {t.lower() for t in _IDENT.findall(text) if _distinctive(t, long_words=long_words)}


def _shape_terms(text: str) -> set[str]:
    out = {p.lower() for p in _PATH.findall(text) if _is_path(p)}
    return out | {h.lower() for h in _HOST.findall(text) if _is_host(h)}


def leak_terms(case: Mapping) -> set[str]:
    """Lowercase terms that would identify the source. From the diff (its code): compound identifiers and long words.
    From reviewer text, messages and comment bodies (prose): compound identifiers and whatever they quote in
    backticks, since a plain long English word there says nothing about the project. From both: paths and host
    names. Plus the repository."""
    code = str(case.get("diff") or "")
    prose = "\n".join(
        _strings(case.get("reviewer_outputs")) + _strings(case.get("aggregator")) + _strings(case.get("expected_posted"))
        + [str(lab.get(k) or "") for lab in case.get("labels") or [] if isinstance(lab, dict) for k in ("file", "message")]
        + _strings(case.get("misses")))
    # prose: compound identifiers (a reviewer or a person naming code), but not a plain long English word
    terms = _code_terms(code) | _shape_terms(code) | _shape_terms(prose) | _code_terms(prose, long_words=False)
    for quoted in _BACKTICKED.findall(prose):
        terms |= _code_terms(quoted)  # what a message quotes in backticks is code, long plain words included
    repo = str(case.get("repo") or "")
    if "/" in repo:
        owner, name = (part.lower() for part in repo.split("/", 1))
        terms |= {repo.lower()} | {part for part in (owner, name) if len(part) >= 4}
    return {t for t in terms if len(t) >= 4}


def _long_lines(texts: list[str]) -> set[str]:
    return {n for t in texts for line in t.splitlines() if len(n := _normal(line)) >= MIN_LINE}


def _body(snippet: Mapping) -> tuple[str, list[str]]:
    """The text a leak could be in (name, paths, file contents), lowercased, and the file contents alone. Base and
    change are kept apart in the second: a change that rewrites a base file must not hide what the base held."""
    contents = [*snippet.get("base", {}).values(), *snippet.get("change", {}).values()]
    paths = [*snippet.get("base", {}), *snippet.get("change", {})]
    return "\n".join([str(snippet.get("name", "")), *paths, *contents]).lower(), contents


def _allowed(allow: Collection[str], brief: Mapping | None) -> set[str]:
    vouched = (brief or {}).get("allow") or []
    return {str(a).strip().lower() for a in [*allow, *(vouched if isinstance(vouched, list) else [])]}


def shared_terms(snippet: Mapping, source: Mapping, *, brief: Mapping | None = None, allow: Collection[str] = ()) -> list[str]:
    """The source's distinctive terms the snippet shares, minus the ones a person allowed (here or in the brief)."""
    lowered, _ = _body(snippet)
    return sorted(t for t in leak_terms(source) - _allowed(allow, brief) if t in lowered)


def check_synthetic(snippet: Mapping, source: Mapping, *, brief: Mapping | None = None, allow: Collection[str] = ()) -> list[str]:
    """Why this snippet may not stand in for the source, or [] when it may."""
    schema = snippet_problems(dict(snippet))
    if schema:
        return schema
    problems = []
    if snippet.get("calibrated") is True:
        problems.append("calibrated must be absent or false: run `eval --live --only <name>` twice before setting it")
    _, contents = _body(snippet)
    problems += [f"shares {t!r} with the source (use another name, or allow it if it is a generic API name)"
                 for t in shared_terms(snippet, source, brief=brief, allow=allow)]
    copied = _long_lines(contents) & _long_lines([str(source.get("diff") or "")])
    problems += [f"copies a line from the source: {line[:60]!r}" for line in sorted(copied)]
    if brief is not None:
        problems += _against_brief(snippet, brief)
    return problems


def _names(category) -> set[str]:
    return {category.lower()} if isinstance(category, str) else {c.lower() for c in category or [] if isinstance(c, str)}


def _against_brief(snippet: Mapping, brief: Mapping) -> list[str]:
    findings = [f for f in brief.get("findings") or [] if isinstance(f, dict)]
    expected = [f for f in findings if f.get("label") == "expected"]
    got = [e for e in snippet.get("expected") or [] if isinstance(e, dict)]
    out = []
    language = brief.get("language")
    if language and language != "unknown":
        used = _language(list(snippet.get("change", {})))
        if used not in (language, "unknown"):
            out.append(f"the brief is for {language}, but the snippet's files are {used}")
    # severity is advisory: it was set by the original's context (a lost shutdown was a suggestion there and a
    # critical when a snippet stood alone), so only the number and the categories must match
    if len(expected) != len(got):
        out.append(f"the brief expects {len(expected)} expected finding(s); the snippet has {len(got)}")
    for f in expected:
        if f.get("category") and not any(str(f["category"]).lower() in _names(e.get("category")) for e in got):
            out.append(f"no expected finding of the brief's category {f['category']!r} (list it among the snippet's categories)")
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
