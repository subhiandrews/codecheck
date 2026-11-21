"""Decide which optional reviewers a change earns (no LLM).

Only the architect (dropped for a clearly small change), the performance reviewer and the docs reviewer
(added when the diff shows a reason) are routed. Forcing a reviewer on, and the `full` mode, are decided by
the caller and always win over this.
"""

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from codecheck.config import RoutingConfig
from codecheck.core.diffmap import FileDiff
from codecheck.core.doccheck import DocMetrics, Scan, measure, scan

STRUCTURAL_NAMES = {
    "pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "package.json", "tsconfig.json", "Cargo.toml", "go.mod", "go.sum",
    "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "build.sbt", "Package.swift",
    "Gemfile", "composer.json", "Makefile", "makefile", "GNUmakefile", "CMakeLists.txt", "BUILD", "BUILD.bazel", "WORKSPACE",
    "MODULE.bazel", "Jenkinsfile", ".gitlab-ci.yml", ".travis.yml", "azure-pipelines.yml", "bitbucket-pipelines.yml",
    "__init__.py", "lib.rs", "mod.rs", "index.ts",
}
STRUCTURAL_SUFFIXES = {".proto", ".graphql", ".gql", ".thrift", ".csproj", ".fsproj", ".vbproj", ".sln", ".h", ".hpp"}
STRUCTURAL_DIRS = (".github/workflows/", ".circleci/", "/migrations/", "/db/migrate/")
_STRUCTURAL_NAME = re.compile(r"^(?:requirements.*\.txt|Dockerfile.*|.*\.Dockerfile|.*\.d\.ts|openapi.*|swagger.*)$", re.IGNORECASE)

_TEST_PATH = re.compile(r"(?:^|/)(?:tests?|__tests__|spec|specs)/|(?:^|/)test_[^/]*$|_test\.\w+$|\.(?:test|spec)\.\w+$")
_LOOP_HEAD = re.compile(r"^\s*(?:async\s+)?(?:for|foreach|while)\b|^\s*loop\s*\{")
_LOOP_CALL = re.compile(r"\.(?:forEach|each|each_with_index)\b|\[[^\]]{0,200}\bfor\b[^\]]{0,200}\bin\b")
_INLINE_BODY = re.compile(r"\{(.*)$|:\s*(\S.*)$")  # the body of a one-line loop: `for (...) { q(); }` or `for x in y: q()`
_QUERY = re.compile(r"\b(?:SELECT|INSERT|UPDATE|DELETE)\b|\.(?:execute|query|fetchone|fetchall|findAll|findOne|save)\(|\b(?:requests|http|fetch|subprocess)\b|\bopen\(|readFile")
_SLOW_IN_LOOP = re.compile(r"\bsorted\(|\bre\.compile\(|\bPattern\.compile\(|\bnew RegExp\(|\bdeepcopy\(|\.copy\(\)|\.sort\(|Collections\.sort|\.index\(|\.indexOf\(|\.includes\(|\.contains\(|\+=\s*f?[\"']")
_AWAIT = re.compile(r"\bawait\b")
_ASYNC_HEAD = re.compile(r"^\s*(?:export\s+)?(?:pub(?:\([\w:]+\))?\s+)?async\s+(?:def|function|fn)\b|=\s*async\b.*=>")
_BLOCKING = re.compile(r"\btime\.sleep\(|\bThread\.sleep\(|\bsubprocess\.(?:run|call|check_output|check_call)\(|"
                       r"\brequests\.(?:get|post|put|patch|delete|request)\(|\b\w+Sync\(")
_MEMBERSHIP = re.compile(r"\b(?:if|elif|while|and|or|not|assert)\b[^#\n]{0,120}?\bin\s+([A-Za-z_][\w.]*)\s*(?:[:)]|$|\band\b|\bor\b)")
_LISTY = re.compile(r"^\s*(\w+)\s*(?::[^=]+)?=\s*(?:\[|list\(|tuple\(|sorted\()")
_HASHED = re.compile(r"^\s*(\w+)\s*(?::[^=]+)?=\s*(?:set\(|frozenset\(|dict\(|defaultdict\(|Counter\(|\{)")
_UNBOUNDED = re.compile(r"\.read\(\)|\.readlines\(\)|read_text\(|readFile(?:Sync)?\(|\.fetchall\(|\.objects\.all\(\)|read_to_string|ReadAll")
_GROWTH = re.compile(r"maxsize\s*=\s*None|@(?:functools\.)?cache\b|\b_?cache\s*(?::[^=]+)?=\s*(?:\{\}|dict\(\)|\[\])")
_STRUCTURE = re.compile(r"\b(?:defaultdict|deque|heapq|PriorityQueue|TreeMap|HashMap|ConcurrentHashMap|BTreeMap|DataFrame|ndarray)\b")
_DDL = re.compile(r"^\s*(?:CREATE|ALTER|DROP)\s+(?:UNIQUE\s+)?(?:INDEX|TABLE)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Routing:
    architect: bool
    performance: bool
    docs: bool
    reasons: tuple[str, ...]
    doc_metrics: DocMetrics


def _structural(path: str) -> bool:
    p = PurePosixPath(path)
    return (p.name in STRUCTURAL_NAMES or p.suffix in STRUCTURAL_SUFFIXES or bool(_STRUCTURAL_NAME.match(p.name))
            or any(d in f"/{path}" for d in STRUCTURAL_DIRS))


def _architect(diffs: Mapping[str, FileDiff], cfg: RoutingConfig) -> tuple[bool, str]:
    changed = sum(1 for fd in diffs.values() for ln in fd.lines if ln.kind != "ctx")
    files = len(diffs)
    if changed > cfg.small_max_lines or files > cfg.small_max_files:
        return True, f"architect: kept, {changed} changed lines in {files} files"
    if any(fd.status != "modified" for fd in diffs.values()):
        return True, "architect: kept, a file was added, deleted or renamed"
    if any(_structural(p) for p in diffs):
        return True, "architect: kept, the change touches a manifest, pipeline or interface file"
    return False, f"architect: skipped, small change ({changed} lines in {files} files)"


def _performance_signals(diffs: Mapping[str, FileDiff], languages: Mapping[str, str | None], scans: Scan) -> Counter:
    signals: Counter = Counter()
    for path, fd in diffs.items():
        language = languages.get(path)
        if not language or fd.binary or fd.status == "deleted" or _TEST_PATH.search(path):  # tests do not earn a review
            continue
        added_sql = False
        ddl = 0
        for run in scans.get(path, ()):
            stack: list[int] = []  # indents of the loop headers that enclose the current line
            asyncs: list[int] = []  # the same for async function headers
            hashed: set[str] = set()  # names built as a set or dict in this run: a membership test on them is cheap
            listy: set[str] = set()  # names built as a list in this run: a membership test on them is linear
            for ln, flag in run:
                if flag is None or flag:  # blank or comment: never a signal, never closes a loop
                    continue
                text = ln.text
                expanded = text.expandtabs(4)
                indent = len(expanded) - len(expanded.lstrip())
                while stack and indent <= stack[-1]:
                    stack.pop()
                while asyncs and indent <= asyncs[-1]:
                    asyncs.pop()
                in_loop, in_async = bool(stack), bool(asyncs)
                head = bool(_LOOP_HEAD.match(text))
                if head:
                    stack.append(indent)
                if _ASYNC_HEAD.match(text):
                    asyncs.append(indent)
                if m := _HASHED.match(text):
                    hashed.add(m.group(1))
                if m := _LISTY.match(text):
                    listy.add(m.group(1))
                if ln.kind != "add":
                    continue
                if language == "sql":
                    added_sql = True
                    if _DDL.match(text) and ddl < 2:
                        ddl += 1
                        signals["schema change"] += 1
                body = (m.group(1) or m.group(2) or "") if head and (m := _INLINE_BODY.search(text)) else ""
                if body and _QUERY.search(body):
                    signals["query or I/O in a loop"] += 3
                if body and _SLOW_IN_LOOP.search(body):
                    signals["linear scan, sort or string build in a loop"] += 2
                if head and in_loop:
                    signals["nested loop"] += 3
                elif (head or _LOOP_CALL.search(text)) and signals["loop"] < 2:
                    signals["loop"] += 1
                if in_async and _BLOCKING.search(text) and signals["blocking call in async code"] < 6:
                    signals["blocking call in async code"] += 3
                if in_loop and not head:
                    if _AWAIT.search(text) and signals["sequential await in a loop"] < 4:
                        signals["sequential await in a loop"] += 2
                    if (language == "python" and (m := _MEMBERSHIP.search(text)) and m.group(1) not in hashed
                            and signals["list membership in a loop"] < 4):
                        # a name known to be a list scores fully; an unknown one (a parameter, an attribute) may be a set
                        step = 2 if m.group(1) in listy else 1
                        signals["list membership in a loop"] = min(4, signals["list membership in a loop"] + step)
                    if _QUERY.search(text):
                        signals["query or I/O in a loop"] += 3
                    if _SLOW_IN_LOOP.search(text):
                        signals["linear scan, sort or string build in a loop"] += 2
                if _UNBOUNDED.search(text) and not signals["unbounded read"]:  # one read is a hint, not a pile of them
                    signals["unbounded read"] += 2
                if _GROWTH.search(text):
                    signals["unbounded cache"] += 2
                if _STRUCTURE.search(text) and signals["heavy data structure"] < 2:
                    signals["heavy data structure"] += 1
        if added_sql:
            signals["SQL file"] += 2
    return signals


def route(diffs: Mapping[str, FileDiff], languages: Mapping[str, str | None], cfg: RoutingConfig) -> Routing:
    scans = scan(diffs, languages)
    metrics = measure(diffs, languages, scans)
    if cfg.mode == "off":
        return Routing(True, False, False, ("routing is off: fixed panel",), metrics)
    architect, why_architect = _architect(diffs, cfg)
    signals = _performance_signals(diffs, languages, scans)
    score = sum(signals.values())
    performance = score >= cfg.performance_min_score
    named = ", ".join(sorted(signals, key=lambda k: -signals[k])) or "no signals"
    why_performance = (f"performance: added, score {score} ({named})" if performance
                       else f"performance: not needed, score {score} below {cfg.performance_min_score} ({named})")
    if metrics.doc_files:
        docs, why_docs = True, f"docs: added, {len(metrics.doc_files)} documentation file(s) changed"
    else:
        docs, why_docs = False, "docs: not needed, no documentation file changed"
    return Routing(architect, performance, docs, (why_architect, why_performance, why_docs), metrics)
