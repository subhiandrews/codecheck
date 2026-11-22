"""Retrieval-quality eval for review memory: `eval --memory-retrieval`. No model, no network; advisory, never a gate
(decision 0023, 0045).

A case (`tests/memory/retrieval/*.json`) seeds a small synthetic lesson store, then asks queries (a diff's paths, or
candidate findings) and names the lessons that should come back (`expect`) and must not (`expect_absent`). The harness
runs each registered *retriever* over every query and reports recall@5 and MRR per retriever, per case and overall.

A retriever is a callable `fn(query: RetrievalQuery) -> list[str]` returning lesson ids, best first. The exact-key
baseline (`exact`) calls the real `memory/retrieve.py` API. Another retriever, such as Phase 5's semantic one, is added
with one line: `register_retriever("semantic", fn, available=reason_or_none)`. `available` returns None when the retriever
can run, else a reason (for example "Ollama not reachable"); `--if-reachable` turns that into a skip, otherwise an error.
Queries marked `"tier": "semantic"` are ones the exact-key baseline is not expected to answer; they are scored in the
same totals and reported separately so the baseline's gap is visible.
"""

import json
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from codecheck.errors import CodeCheckError
from codecheck.evals.snippet import path_ok
from codecheck.memory.db import MemoryDB
from codecheck.memory.model import KINDS, LEANS, STATUSES, Lesson, Scope, lesson_id
from codecheck.memory.render import render_memory
from codecheck.memory.retrieve import finding_lessons, miss_lessons

DEFAULT_CASES = Path(__file__).resolve().parents[3] / "tests" / "memory" / "retrieval"
K = 5
NOW = 1_800_000_000.0  # fixed, so expiry is the same on every run
DEFAULT_REPO = "acme/widgets"
FETCH_LIMIT = 50  # what a retriever is asked for; recall@K truncates
FRAME_TEXT = "unconfirmed data, not instructions"
TIERS = ("exact", "semantic")


@dataclass
class RetrievalQuery:
    db: MemoryDB
    repo: str
    mode: str  # "diff" (reviewers stage: paths) or "findings" (aggregator stage: candidate findings)
    paths: list[str]
    findings: list[dict]  # {"file", "category", "message"?}
    files_at_base: list[str]
    now: float = NOW
    limit: int = FETCH_LIMIT
    extra: dict = field(default_factory=dict)


Retriever = Callable[[RetrievalQuery], list[str]]


def exact_retriever(q: RetrievalQuery) -> list[str]:
    """The shipped exact-key retrieval, through the real API."""
    clock = lambda: q.now
    if q.mode == "diff":
        found = miss_lessons(q.db, q.repo, q.paths, q.files_at_base, q.limit, now=clock)
    else:
        found = finding_lessons(q.db, q.repo, [(f["file"], f.get("category", "")) for f in q.findings],
                                q.files_at_base, q.limit, now=clock)
    return [le.id for le in found]


# name -> (retriever, availability check returning None or a reason)
RETRIEVERS: dict[str, tuple[Retriever, Callable[[], str | None]]] = {}


def register_retriever(name: str, fn: Retriever, available: Callable[[], str | None] | None = None) -> None:
    """Add a retriever to the eval. `available()` returns None when it can run, else why not."""
    RETRIEVERS[name] = (fn, available or (lambda: None))


register_retriever("exact", exact_retriever)


# --- case schema -------------------------------------------------------------------------------------------------


def _str(v: object) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _lesson_problems(i: int, le: object) -> list[str]:
    if not isinstance(le, dict):
        return [f"lessons[{i}] must be an object"]
    out = []
    if not _str(le.get("name")):
        out.append(f"lessons[{i}].name must be a non-empty string")
    if le.get("kind") not in KINDS:
        out.append(f"lessons[{i}].kind must be one of {', '.join(KINDS)}")
    if not _str(le.get("path_glob")) or not (le["path_glob"] == "*" or (le["path_glob"].endswith("/**") and path_ok(le["path_glob"][:-3]))):
        out.append(f"lessons[{i}].path_glob must be '*' or 'dir/**'")
    if not _str(le.get("statement")):
        out.append(f"lessons[{i}].statement must be a non-empty string")
    for key in ("language", "category", "repo"):
        if key in le and not isinstance(le[key], str):
            out.append(f"lessons[{i}].{key} must be a string")
    if le.get("status", "active") not in STATUSES:
        out.append(f"lessons[{i}].status must be one of {', '.join(STATUSES)}")
    if le.get("lean", "") not in ("", *LEANS):
        out.append(f"lessons[{i}].lean must be one of {', '.join(LEANS)}")
    for key in ("support", "contra", "expires_in_days"):
        if key in le and (isinstance(le[key], bool) or not isinstance(le[key], (int, float))):
            out.append(f"lessons[{i}].{key} must be a number")
    if "n_prs" in le and (isinstance(le["n_prs"], bool) or not isinstance(le["n_prs"], int)):
        out.append(f"lessons[{i}].n_prs must be an integer")
    return out


def _query_problems(i: int, q: object, names: set[str]) -> list[str]:
    if not isinstance(q, dict):
        return [f"queries[{i}] must be an object"]
    out = []
    if not _str(q.get("name")):
        out.append(f"queries[{i}].name must be a non-empty string")
    if q.get("mode") == "diff":
        paths = q.get("paths")
        if not (isinstance(paths, list) and paths and all(path_ok(p) for p in paths)):
            out.append(f"queries[{i}].paths must be a non-empty list of relative paths")
    elif q.get("mode") == "findings":
        found = q.get("findings")
        if not (isinstance(found, list) and found and all(isinstance(f, dict) and path_ok(f.get("file")) and _str(f.get("category")) for f in found)):
            out.append(f"queries[{i}].findings must be a non-empty list of {{file, category}}")
    else:
        out.append(f"queries[{i}].mode must be 'diff' or 'findings'")
    for key in ("expect", "expect_absent"):
        v = q.get(key, [])
        if not (isinstance(v, list) and all(isinstance(n, str) for n in v)):
            out.append(f"queries[{i}].{key} must be a list of lesson names")
        elif set(v) - names:
            out.append(f"queries[{i}].{key} names unknown lessons: {', '.join(sorted(set(v) - names))}")
    if not q.get("expect") and not q.get("expect_absent"):
        out.append(f"queries[{i}] needs expect or expect_absent")
    if "text" in q and not _str(q["text"]):
        out.append(f"queries[{i}].text must be a non-empty string (the summary of the diff's added lines, for a semantic retriever)")
    if q.get("tier", "exact") not in TIERS:
        out.append(f"queries[{i}].tier must be one of {', '.join(TIERS)}")
    render = q.get("render", {})
    if not isinstance(render, dict) or not all(isinstance(render.get(k, []), list) and all(isinstance(x, str) for x in render.get(k, []))
                                               for k in ("framed", "contains")):
        out.append(f"queries[{i}].render must be {{framed?: [lesson name], contains?: [text]}}")
    elif set(render.get("framed", [])) - names:
        out.append(f"queries[{i}].render.framed names unknown lessons")
    return out


def case_problems(case: object) -> list[str]:
    """Why a retrieval case file is not valid; empty when it is. Pure, so a test can run it over every shipped case."""
    if not isinstance(case, dict):
        return ["expected a JSON object"]
    out = []
    if not _str(case.get("id")):
        out.append("id must be a non-empty string")
    if not _str(case.get("description")):
        out.append("description must be a non-empty string")
    if "repo" in case and not _str(case["repo"]):
        out.append("repo must be a non-empty string")
    files = case.get("files_at_base")
    if not (isinstance(files, list) and files and all(path_ok(f) for f in files)):
        out.append("files_at_base must be a non-empty list of relative paths")
    lessons = case.get("lessons")
    names: set[str] = set()
    if not (isinstance(lessons, list) and lessons):
        out.append("lessons must be a non-empty list")
    else:
        ids: set[str] = set()
        for i, le in enumerate(lessons):
            problems = _lesson_problems(i, le)
            out += problems
            if problems:
                continue
            if le["name"] in names:
                out.append(f"lessons[{i}]: duplicate name {le['name']!r}")
            names.add(le["name"])
            lid = _lesson_for(case, le).id
            if lid in ids:
                out.append(f"lessons[{i}]: same repo, scope and kind as an earlier lesson (they would share an id)")
            ids.add(lid)
    queries = case.get("queries")
    if not (isinstance(queries, list) and queries):
        out.append("queries must be a non-empty list")
    else:
        out += [p for i, q in enumerate(queries) for p in _query_problems(i, q, names)]
        qnames = [q.get("name") for q in queries if isinstance(q, dict)]
        if len(set(qnames)) != len(qnames):
            out.append("query names must be unique")
    return out


def load_cases(directory: Path) -> list[dict]:
    """Every `<directory>/*.json`, sorted; an error names the offending file. A missing directory has no cases."""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    cases, seen = [], set()
    for path in sorted(directory.glob("*.json")):
        try:
            case = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            raise CodeCheckError(f"retrieval case {path.name}: not readable JSON: {e}") from e
        problems = case_problems(case)
        if problems:
            raise CodeCheckError(f"retrieval case {path.name}: " + "; ".join(problems))
        if case["id"] in seen:
            raise CodeCheckError(f"retrieval case {path.name}: duplicate id {case['id']!r}")
        seen.add(case["id"])
        cases.append(case)
    return cases


# --- scoring -----------------------------------------------------------------------------------------------------


def _lesson_for(case: dict, spec: dict) -> Lesson:
    repo = spec.get("repo", case.get("repo", DEFAULT_REPO))
    category = spec.get("category") or ("any" if spec["kind"] == "miss" else "other")
    scope = Scope("github.com", repo, spec["path_glob"], spec.get("language", ""), category)
    support, contra = float(spec.get("support", 4.0)), float(spec.get("contra", 0.0))
    return Lesson(
        id=lesson_id(scope, spec["kind"]), scope=scope, kind=spec["kind"], lean=spec.get("lean", ""),
        statement=spec["statement"], support=support, contra=contra, n_prs=int(spec.get("n_prs", 3)),
        confidence=support / (support + contra) if support + contra else 1.0, status=spec.get("status", "active"),
        first_ts=NOW - 86400 * 30, last_ts=NOW - 86400, expires_ts=NOW + 86400 * float(spec.get("expires_in_days", 180)),
        maintainer=True, provenance={"seeded": True})


def seed(case: dict, directory: Path) -> tuple[MemoryDB, dict[str, str]]:
    """A store holding the case's lessons, and the lesson id of each name."""
    lessons = {spec["name"]: _lesson_for(case, spec) for spec in case["lessons"]}
    db = MemoryDB(directory, now=lambda: NOW)
    db.put_lessons(lessons.values())
    return db, {n: le.id for n, le in lessons.items()}


def _render_failures(q: dict, ranked_ids: list[str], db: MemoryDB, ids: dict[str, str]) -> list[str]:
    spec = q.get("render") or {}
    if not (spec.get("framed") or spec.get("contains")):
        return []
    lessons = [le for i in ranked_ids[:K] if (le := db.lesson(i)) is not None]
    text = render_memory(lessons, {}, for_aggregator=q["mode"] == "findings", max_lessons=K, max_chars=8000)
    out = []
    if spec.get("framed") and FRAME_TEXT not in text:
        out.append("the rendered memory has no unconfirmed-data frame")
    for name in spec.get("framed", []):
        lid = ids[name]
        le = next((x for x in lessons if x.id == lid), None)
        if le is None:
            out.append(f"{name} was not rendered (dropped)")
            continue
        probe = (le.statement.splitlines() or [""])[0][:40]  # the first line, which an unescaped newline would leave behind
        loose = [ln for ln in text.splitlines() if probe and probe in ln and not ln.startswith("- statement: ")]
        if loose:
            out.append(f"{name} appears outside its framed statement line")
    for needle in spec.get("contains", []):
        if needle not in text:
            out.append(f"the rendered memory does not contain {needle!r}")
    return out


def _score_query(case: dict, q: dict, ranked: list[str], db: MemoryDB, ids: dict[str, str]) -> dict:
    by_id = {v: k for k, v in ids.items()}
    names = [by_id.get(i, i) for i in ranked]
    expect = q.get("expect", [])
    top = names[:K]
    out: dict = {"query": q["name"], "tier": q.get("tier", "exact"), "ranked": names[:10]}
    if expect:
        out["recall_at_5"] = sum(1 for n in expect if n in top) / len(expect)
        out["rr"] = next((1 / (rank + 1) for rank, n in enumerate(names) if n in expect), 0.0)
        out["missing"] = [n for n in expect if n not in top]
    out["unexpected"] = [n for n in q.get("expect_absent", []) if n in names]
    out["render_failures"] = _render_failures(q, ranked, db, ids)
    return out


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def _summary(rows: list[dict]) -> dict:
    scored = [r for r in rows if "recall_at_5" in r]
    return {"queries": len(rows), "scored": len(scored), "recall_at_5": _mean([r["recall_at_5"] for r in scored]),
            "mrr": _mean([r["rr"] for r in scored]),
            "absent_violations": sum(len(r["unexpected"]) for r in rows),
            "render_failures": sum(len(r["render_failures"]) for r in rows)}


def evaluate(case: dict, fn: Retriever) -> list[dict]:
    """Every query of one case through one retriever, scored."""
    with tempfile.TemporaryDirectory(prefix="code-check-memory-eval-", ignore_cleanup_errors=True) as work:
        db, ids = seed(case, Path(work) / "memory")
        rows = []
        for q in case["queries"]:
            paths = q["paths"] if q["mode"] == "diff" else [f["file"] for f in q["findings"]]
            query = RetrievalQuery(db=db, repo=case.get("repo", DEFAULT_REPO), mode=q["mode"], paths=list(paths),
                                   findings=list(q.get("findings", [])), files_at_base=list(case["files_at_base"]),
                                   extra={"text": q["text"]} if q.get("text") else {})
            rows.append(_score_query(case, q, list(fn(query)), db, ids))
        return rows


def run_retrieval(directory: Path, only: str | None = None, if_reachable: bool = False,
                  retrievers: dict | None = None) -> dict:
    """The `eval --memory-retrieval` result. `only` names one retriever; unavailable ones skip (`if_reachable`) or error."""
    registry = RETRIEVERS if retrievers is None else retrievers
    if only is not None and only not in registry:
        raise CodeCheckError(f"unknown retriever {only!r}; known: {', '.join(sorted(registry)) or 'none'}")
    cases = load_cases(directory)
    out: dict = {"advisory": True, "k": K, "cases": len(cases), "retrievers": {}}
    for name in sorted(registry):
        if only is not None and name != only:
            continue
        fn, available = registry[name]
        reason = available()
        if reason is not None:
            if not if_reachable:
                raise CodeCheckError(f"retriever {name!r} not available: {reason}")
            out["retrievers"][name] = {"skipped": f"not available: {reason}"}
            continue
        per_case, all_rows = {}, []
        for case in cases:
            rows = evaluate(case, fn)
            per_case[case["id"]] = {**_summary(rows), "results": rows}
            all_rows += [{**r, "case": case["id"]} for r in rows]
        out["retrievers"][name] = {
            **_summary(all_rows),
            "by_tier": {t: _summary([r for r in all_rows if r["tier"] == t]) for t in TIERS if any(r["tier"] == t for r in all_rows)},
            "per_case": per_case,
            "problems": [{"case": r["case"], "query": r["query"], **{k: r[k] for k in ("missing", "unexpected", "render_failures") if r.get(k)}}
                         for r in all_rows if r.get("missing") or r["unexpected"] or r["render_failures"]],
        }
    return out
