"""Live snippet eval: run the real review-local flow through `claude -p` on small snippets and match its findings.

Each snippet gets a throwaway git repository (a bare origin with the base committed on `main`, and a clone holding
the change as an uncommitted edit) and one headless review, the way `run-pr` shells out. The run is isolated with
`CODE_CHECK_` environment assignments: state and the shadow store live under the snippet's work directory and
hand-off is on, so the aggregator's raw output can be read back and nothing touches the user's own state, shadow
store or review bookkeeping. This costs real tokens; it is local developer tooling and never runs in CI.
"""

import json
import time
from pathlib import Path

from codecheck.commands.run_pr import parse_result
from codecheck.core.findings import merge_exact_duplicates, validate_findings
from codecheck.core.paths import contained, finding_items
from codecheck.errors import CodeCheckError
from codecheck.evals.match import match_case
from codecheck.evals.snippet import path_ok, snippet_problems
from codecheck.memory.db import MemoryDB
from codecheck.memory.model import DEFAULT_HOST, Lesson, Scope, lesson_id
from codecheck.runner import run_checked
from codecheck.shadow.store import ShadowStore

# The plugin repository's own snippets; an installed package has no `tests/`.
DEFAULT_SNIPPETS = Path(__file__).resolve().parents[3] / "tests" / "snippets"
REVIEW_PROMPT = ("Use the code-check review-local skill. Run unattended: never ask questions; "
                 "finish with one line summarizing the outcome.")
PROBE_TIMEOUT = 60
# Variables that make git work on another repository than the one in its cwd, or inject config. Git exports several
# of them to hooks (a pre-commit hook runs this eval), so they are unset for the setup and for the headless review:
# otherwise the setup would commit into, and push from, the user's own repository.
GIT_REPO_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_PREFIX", "GIT_CONFIG",
                 "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_QUARANTINE_PATH")
UNSET_GIT_REPO_VARS = tuple(a for name in GIT_REPO_VARS for a in ("-u", name))
# Fixed identity and no user hooks or signing for the throwaway repositories, whatever the user's git config says.
_GIT = ("env", *UNSET_GIT_REPO_VARS, "git", "-c", "user.name=code-check eval", "-c", "user.email=eval@code-check.invalid",
        "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "-c", "init.defaultBranch=main")


def plugin_root(env) -> Path:
    """`$CLAUDE_PLUGIN_ROOT` when set, else the repository root two levels above `src/`."""
    given = env.get("CLAUDE_PLUGIN_ROOT")
    return Path(given).expanduser().resolve() if given else Path(__file__).resolve().parents[3]


# --- snippets ----------------------------------------------------------------------------------------------------


def load_snippets(directory: Path) -> list[dict]:
    """Every `<directory>/<snippet>/case.json`, sorted by folder; an error names the offending file."""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    snippets, seen = [], set()
    for path in sorted(directory.glob("*/case.json")):
        where = f"{path.parent.name}/{path.name}"
        try:
            snippet = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            raise CodeCheckError(f"snippet {where}: not readable JSON: {e}") from e
        problems = snippet_problems(snippet)
        if problems:
            raise CodeCheckError(f"snippet {where}: " + "; ".join(problems))
        if snippet["name"] in seen:
            raise CodeCheckError(f"snippet {where}: duplicate name {snippet['name']!r}")
        seen.add(snippet["name"])
        snippets.append({"must_not_report": [], **snippet})
    return snippets


# --- the model -------------------------------------------------------------------------------------------------


def claude_bin(ctx) -> str:
    binary = ctx.config.run_pr.claude_bin
    # After `env`, a leading '-' would be an env option and an '=' would make it another assignment.
    if not binary.strip() or binary.lstrip().startswith("-") or "=" in binary:
        raise CodeCheckError(f"run_pr.claude_bin {binary!r} cannot be used: must not start with '-' or contain '='")
    return binary


def unreachable_reason(ctx) -> str | None:
    """None when a cheap `claude -p` probe answers with non-error JSON, else why not."""
    argv = [claude_bin(ctx), "-p", "reply with ok", "--output-format", "json", "--permission-mode", "dontAsk"]
    res = ctx.runner.run(argv, timeout=PROBE_TIMEOUT, input="")  # input="" closes stdin
    try:
        data = json.loads(res.stdout)
    except (json.JSONDecodeError, TypeError):
        data = None
    if isinstance(data, dict) and data.get("is_error") is False:
        return None
    if isinstance(data, dict):
        return f"probe returned an error: {str(data.get('result', ''))[:300]}"
    return f"probe exited {res.returncode}: {(res.stderr.strip() or res.stdout.strip())[:300]}"


def claude_reachable(ctx) -> bool:
    return unreachable_reason(ctx) is None


# --- one snippet -------------------------------------------------------------------------------------------------


def _git(ctx, cwd: Path, *args: str) -> None:
    run_checked(ctx.runner, [*_GIT, *args], cwd=cwd)


def _write_files(repo: Path, files: dict) -> None:
    for rel, text in files.items():
        target = contained(repo, rel) if path_ok(rel) else None
        if target is None:  # load_snippets already refused these; this also stops a symlink in the base
            raise CodeCheckError(f"snippet path {rel!r} escapes the repository")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _setup_repo(ctx, snippet: dict, workdir: Path) -> Path:
    """Bare origin with `base` committed on main, and a clone with `change` applied but not committed."""
    origin, seed, repo = workdir / "origin.git", workdir / "seed", workdir / "repo"
    _git(ctx, workdir, "init", "--quiet", "--bare", "-b", "main", str(origin))
    _git(ctx, workdir, "clone", "--quiet", str(origin), str(seed))
    _write_files(seed, snippet["base"])
    _git(ctx, seed, "add", "-A")  # never file names: nothing from the snippet goes into argv
    _git(ctx, seed, "commit", "--quiet", "--allow-empty", "-m", "base")
    _git(ctx, seed, "push", "--quiet", "origin", "HEAD:main")
    _git(ctx, workdir, "clone", "--quiet", str(origin), str(repo))  # a fresh clone knows origin/HEAD is main
    _write_files(repo, snippet["change"])
    return repo


def _seed_memory(memory: dict, workdir: Path, repo: str) -> None:
    """Put the snippet's own lessons, active and well supported, into a store in its workdir."""
    now = time.time()
    lessons = []
    for spec in memory["lessons"]:
        category = spec.get("category") or ("any" if spec["kind"] == "miss" else "other")
        scope = Scope(DEFAULT_HOST, repo, spec["path_glob"], spec.get("language", ""), category)
        lessons.append(Lesson(
            id=lesson_id(scope, spec["kind"]), scope=scope, kind=spec["kind"], lean=spec.get("lean", ""),
            statement=spec["statement"], support=4.0, contra=0.0, n_prs=3, confidence=1.0, status="active",
            first_ts=now - 86400, last_ts=now, expires_ts=now + 86400 * 180, maintainer=True, provenance={"seeded": True}))
    MemoryDB(workdir / "memory").put_lessons(lessons)


def review_argv(ctx, workdir: Path, *, memory: bool = False) -> list[str]:
    cfg = ctx.config.run_pr
    root = plugin_root(ctx.env)
    # Paths are absolute, so never start with '-'; tools and permission mode are validated with the config.
    isolate = [f"CODE_CHECK_STATE_DIR={workdir}/state", f"CODE_CHECK_SHADOW__STORE={workdir}/shadow",
               "CODE_CHECK_SHADOW__HANDOFF=1",
               # The throwaway repository's base is origin/main, whatever the user configured.
               "CODE_CHECK_REMOTE=origin", "CODE_CHECK_BASE_BRANCH=main",
               # A user's review.security, review.performance or review.full would add an extra agent to every snippet,
               # and routing would drop the architect from these small changes: the eval measures the fixed panel.
               "CODE_CHECK_REVIEW__SECURITY=0", "CODE_CHECK_REVIEW__PERFORMANCE=0", "CODE_CHECK_REVIEW__FULL=0",
               "CODE_CHECK_REVIEW__ROUTING__MODE=off",
               # A user's memory settings would put their lessons into every snippet. Only a snippet that carries lessons
               # of its own (`memory` in its case.json) reads memory, from a store built in its workdir.
               *(["CODE_CHECK_MEMORY__ENABLED=1", "CODE_CHECK_MEMORY__READ=1", "CODE_CHECK_MEMORY__REVIEWERS=1",
                  f"CODE_CHECK_MEMORY__STORE={workdir}/memory"] if memory else ["CODE_CHECK_MEMORY__READ=0"])]
    # The prompt comes first: --allowedTools and --add-dir are variadic and would swallow a later positional.
    return ["env", *UNSET_GIT_REPO_VARS, *isolate, claude_bin(ctx), "-p", REVIEW_PROMPT, "--plugin-dir", str(root),
            "--output-format", "json", "--permission-mode", cfg.permission_mode, "--permission-prompts", "none",
            "--add-dir", str(workdir), "--allowedTools", *cfg.review_allowed_tools]


def _findings(store: ShadowStore) -> tuple[list[dict], list[str], str | None]:
    """The newest hand-off's aggregator findings as (got, validation errors, error)."""
    raw = next((r for rid in reversed(store.ordered_run_ids()) if (r := store.read_raw(rid)) is not None), None)
    if raw is None:
        return [], [], "no hand-off output in the shadow store: the review did not reach its hand-off step"
    if "aggregator" not in raw:
        return [], [], "the hand-off has no aggregator output"
    findings, errors = validate_findings(finding_items(raw["aggregator"]), reviewer="aggregator", root=raw.get("root"))
    got = [{"file": f.file, "line": f.line, "category": f.category, "severity": f.severity.value}
           for f in merge_exact_duplicates(findings)]
    return got, errors, None


def run_snippet(ctx, snippet: dict, workdir: Path) -> dict:
    workdir = Path(workdir).resolve()
    try:
        repo = _setup_repo(ctx, snippet, workdir)
    except CodeCheckError as e:
        raise CodeCheckError(f"snippet {snippet['name']}: could not build its repository: {e}") from e
    if snippet.get("memory"):
        _seed_memory(snippet["memory"], workdir, repo.name)
    res = ctx.runner.run(review_argv(ctx, workdir, memory=bool(snippet.get("memory"))), cwd=repo, timeout=ctx.config.run_pr.timeout_seconds, input="")
    claude = parse_result(res.stdout, res.returncode, res.stderr)
    got, invalid, error = _findings(ShadowStore(workdir / "shadow"))
    if not claude["ok"]:
        error = f"claude run failed: {claude['summary']}"
    if error is None and invalid and not got:
        error = "every aggregator finding was invalid"  # not a clean review, whatever `expected` says
    result = match_case(snippet["expected"], snippet["must_not_report"], got)
    if error:
        result["passed"] = False  # no findings because the run broke is not a clean review
    out = {"name": snippet["name"], "result": result, "got": got,
           "claude": {"ok": claude["ok"], "summary": claude["summary"], "cost_usd": claude["cost"]}}
    if invalid:
        out["invalid"] = invalid[:5]
    if error:
        out["error"] = error
    return out
