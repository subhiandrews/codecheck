"""Live snippet eval: run the real review-local flow through `claude -p` on small snippets and match its findings.

Each snippet gets a throwaway git repository (a bare origin with the base committed on `main`, and a clone holding
the change as an uncommitted edit) and one headless review, the way `run-pr` shells out. The run is isolated with
`CODE_CHECK_` environment assignments: state and the shadow store live under the snippet's work directory and
hand-off is on, so the aggregator's raw output can be read back and nothing touches the user's own state, shadow
store or review bookkeeping. This costs real tokens; it is local developer tooling and never runs in CI.
"""

import json
import re
from pathlib import Path, PurePosixPath

from codecheck.commands.run_pr import parse_result
from codecheck.core.findings import merge_exact_duplicates, validate_findings
from codecheck.core.paths import contained, finding_items
from codecheck.errors import CodeCheckError
from codecheck.evals.match import match_case
from codecheck.runner import run_checked
from codecheck.shadow.store import ShadowStore

# The plugin repository's own snippets; an installed package has no `tests/`.
DEFAULT_SNIPPETS = Path(__file__).resolve().parents[3] / "tests" / "snippets"
REVIEW_PROMPT = ("Use the code-check review-local skill. Run unattended: never ask questions; "
                 "finish with one line summarizing the outcome.")
PROBE_TIMEOUT = 60
SEVERITIES = ("critical", "warning", "suggestion")
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


# Code points HFS+ ignores in names, so `.g\u200cit` is `.git` there (git's own core.protectHFS list).
_HFS_IGNORED = re.compile("[\u200c-\u200f\u202a-\u202e\u206a-\u206f\ufeff]")


def _is_git_dir_name(part: str) -> bool:
    """`.git` however a filesystem may spell it: any case (APFS and NTFS default to case-insensitive), with
    HFS-ignored characters or trailing dots and spaces (NTFS), or as an 8.3 short name like `GIT~1`."""
    name = _HFS_IGNORED.sub("", part).lower().rstrip(". ")
    return name == ".git" or re.fullmatch(r"git~\d+", name) is not None


def _path_ok(rel: object) -> bool:
    if not isinstance(rel, str) or not rel or "\0" in rel or "\\" in rel:
        return False
    p = PurePosixPath(rel)
    return not p.is_absolute() and all(part != ".." and not _is_git_dir_name(part) for part in p.parts)


def _category_ok(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return isinstance(value, list) and bool(value) and all(isinstance(v, str) and v.strip() for v in value)


def _int_line(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _problems(snippet: object) -> list[str]:
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
            if not _path_ok(rel):
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
            if not (isinstance(e, dict) and _path_ok(e.get("file")) and _int_line(e.get("line"))
                    and _category_ok(e.get("category")) and e.get("severity") in SEVERITIES):
                out.append(f"expected[{i}] needs file, line (>= 1), category (a name or a list of names) and "
                           f"severity ({', '.join(SEVERITIES)})")
    rules = snippet.get("must_not_report", [])
    if not isinstance(rules, list):
        out.append("must_not_report must be a list")
    else:
        for i, r in enumerate(rules):
            if not (isinstance(r, dict) and _path_ok(r.get("file"))
                    and (r.get("category") is None or _category_ok(r["category"]))
                    and (r.get("line") is None or _int_line(r["line"]))):
                out.append(f"must_not_report[{i}] needs file, and optionally category and line")
    if "calibrated" in snippet and not isinstance(snippet["calibrated"], bool):
        out.append("calibrated must be true or false")
    return out


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
        problems = _problems(snippet)
        if problems:
            raise CodeCheckError(f"snippet {where}: " + "; ".join(problems))
        if snippet["name"] in seen:
            raise CodeCheckError(f"snippet {where}: duplicate name {snippet['name']!r}")
        seen.add(snippet["name"])
        snippets.append({"must_not_report": [], **snippet})
    return snippets


# --- the model -------------------------------------------------------------------------------------------------


def _claude_bin(ctx) -> str:
    binary = ctx.config.run_pr.claude_bin
    # After `env`, a leading '-' would be an env option and an '=' would make it another assignment.
    if not binary.strip() or binary.lstrip().startswith("-") or "=" in binary:
        raise CodeCheckError(f"run_pr.claude_bin {binary!r} cannot be used: must not start with '-' or contain '='")
    return binary


def unreachable_reason(ctx) -> str | None:
    """None when a cheap `claude -p` probe answers with non-error JSON, else why not."""
    argv = [_claude_bin(ctx), "-p", "reply with ok", "--output-format", "json", "--permission-mode", "dontAsk"]
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
        target = contained(repo, rel) if _path_ok(rel) else None
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


def review_argv(ctx, workdir: Path) -> list[str]:
    cfg = ctx.config.run_pr
    root = plugin_root(ctx.env)
    # Paths are absolute, so never start with '-'; tools and permission mode are validated with the config.
    isolate = [f"CODE_CHECK_STATE_DIR={workdir}/state", f"CODE_CHECK_SHADOW__STORE={workdir}/shadow",
               "CODE_CHECK_SHADOW__HANDOFF=1",
               # The throwaway repository's base is origin/main, whatever the user configured.
               "CODE_CHECK_REMOTE=origin", "CODE_CHECK_BASE_BRANCH=main",
               # A user's review.security would add an Opus security agent to every snippet.
               "CODE_CHECK_REVIEW__SECURITY=0"]
    # The prompt comes first: --allowedTools and --add-dir are variadic and would swallow a later positional.
    return ["env", *UNSET_GIT_REPO_VARS, *isolate, _claude_bin(ctx), "-p", REVIEW_PROMPT, "--plugin-dir", str(root),
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
    res = ctx.runner.run(review_argv(ctx, workdir), cwd=repo, timeout=ctx.config.run_pr.timeout_seconds, input="")
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
