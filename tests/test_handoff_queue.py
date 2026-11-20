import json
import os
import stat

import pytest
from clihelp import call, make_ctx
from gitutil import make_origin_and_clone, sh

from codecheck.cli import build_parser
from codecheck.core.state import FileLock
from codecheck.errors import CodeCheckError
from codecheck.shadow.queue import (
    CLAIM_STALE_SECONDS,
    MAX_ATTEMPTS,
    MAX_DIFF_LINES,
    claim_next,
    finish,
    release,
    summarize_diff,
    violations,
)
from codecheck.shadow.store import ShadowStore

pytestmark = pytest.mark.usefixtures("git_env")

NOW = 1_800_000_000.0


def make_run(shadow: ShadowStore, run_id: str, *, private=False, ts=1.0, handoff=True, pr="o/r#1") -> None:
    shadow.append(run_id, {"type": "run", "pr": pr, "ts": ts, "repo_private": private})
    if handoff:
        (shadow.handoff_dir(run_id) / "handoff.json").write_text("{}", encoding="utf-8")


def nonce(shadow: ShadowStore, run_id: str) -> str:
    try:
        return marker(shadow, run_id).get("nonce", "")
    except Exception:  # noqa: BLE001 - no marker yet
        return ""


def marker(shadow: ShadowStore, run_id: str) -> dict:
    return json.loads((shadow.root / f"{run_id}.handoff" / "analysis.json").read_text(encoding="utf-8"))


@pytest.fixture
def shadow(tmp_path):
    return ShadowStore(tmp_path / "shadow", now=lambda: 1.0)


# -- the queue -----------------------------------------------------------------------------------------------


def test_claims_the_oldest_public_handoff(shadow):
    make_run(shadow, "aaaaaaaaaaaa", ts=30)
    make_run(shadow, "bbbbbbbbbbbb", ts=10)
    make_run(shadow, "cccccccccccc", ts=20)
    out = claim_next(shadow, NOW)
    assert out["run"] == "bbbbbbbbbbbb"
    m = marker(shadow, "bbbbbbbbbbbb")
    assert (m["status"], m["attempts"], m["claimed_ts"]) == ("claimed", 1, NOW)


def test_nothing_pending_returns_no_run(shadow):
    assert claim_next(shadow, NOW) == {"run": None, "skipped": {}}


def test_skips_runs_it_must_not_analyze_and_says_why(shadow):
    make_run(shadow, "aaaaaaaaaaaa", ts=1, handoff=False)       # no hand-off folder
    make_run(shadow, "bbbbbbbbbbbb", ts=2, private=True)        # private repository
    make_run(shadow, "cccccccccccc", ts=3, private=None)        # visibility unknown
    make_run(shadow, "dddddddddddd", ts=4)
    first = claim_next(shadow, NOW)
    finish(shadow, first["run"], "https://github.com/o/r/pull/9", NOW, first["claim"])  # dddd is done
    out = claim_next(shadow, NOW)
    assert out["run"] is None
    assert out["skipped"] == {"no_handoff": 1, "not_public": 2, "done": 1}


def test_a_fresh_claim_is_left_alone_and_a_stale_one_is_retaken(shadow):
    make_run(shadow, "aaaaaaaaaaaa")
    assert claim_next(shadow, NOW)["run"] == "aaaaaaaaaaaa"
    busy = claim_next(shadow, NOW + 60)
    assert busy["run"] is None and busy["skipped"] == {"claimed": 1}
    again = claim_next(shadow, NOW + CLAIM_STALE_SECONDS + 1)
    assert again["run"] == "aaaaaaaaaaaa"
    assert marker(shadow, "aaaaaaaaaaaa")["attempts"] == 2


def test_gives_up_after_max_attempts(shadow):
    make_run(shadow, "aaaaaaaaaaaa")
    for i in range(MAX_ATTEMPTS):
        assert claim_next(shadow, NOW + i)["run"] == "aaaaaaaaaaaa"
        release(shadow, "aaaaaaaaaaaa", "tests failed", NOW + i, nonce(shadow, "aaaaaaaaaaaa"))
    out = claim_next(shadow, NOW + 100)
    assert out["run"] is None and out["skipped"] == {"gave_up": 1}


def test_release_makes_the_run_claimable_again_and_keeps_the_attempt_count(shadow):
    make_run(shadow, "aaaaaaaaaaaa")
    claim_next(shadow, NOW)
    release(shadow, "aaaaaaaaaaaa", "ci red", NOW + 1, nonce(shadow, "aaaaaaaaaaaa"))
    assert marker(shadow, "aaaaaaaaaaaa")["status"] == "released"
    assert claim_next(shadow, NOW + 2)["run"] == "aaaaaaaaaaaa"
    assert marker(shadow, "aaaaaaaaaaaa")["attempts"] == 2


def test_release_scrubs_and_bounds_the_reason(shadow):
    make_run(shadow, "aaaaaaaaaaaa")
    claim_next(shadow, NOW)
    release(shadow, "aaaaaaaaaaaa", "token ghp_" + "a" * 36 + " " + "x" * 5000, NOW, nonce(shadow, "aaaaaaaaaaaa"))
    reason = marker(shadow, "aaaaaaaaaaaa")["reason"]
    assert "ghp_" + "a" * 36 not in reason and len(reason) <= 500


def test_finish_marks_done_and_is_idempotent(shadow):
    make_run(shadow, "aaaaaaaaaaaa")
    claim_next(shadow, NOW)
    finish(shadow, "aaaaaaaaaaaa", "https://github.com/o/r/pull/9", NOW + 5, nonce(shadow, "aaaaaaaaaaaa"))
    finish(shadow, "aaaaaaaaaaaa", "https://github.com/o/r/pull/9", NOW + 9, nonce(shadow, "aaaaaaaaaaaa"))
    m = marker(shadow, "aaaaaaaaaaaa")
    assert (m["status"], m["pr"], m["attempts"]) == ("done", "https://github.com/o/r/pull/9", 1)
    assert claim_next(shadow, NOW + 10)["run"] is None


def test_finish_and_release_need_a_claim(shadow):
    make_run(shadow, "aaaaaaaaaaaa")
    with pytest.raises(CodeCheckError, match="not claimed"):
        finish(shadow, "aaaaaaaaaaaa", "https://github.com/o/r/pull/9", NOW, nonce(shadow, "aaaaaaaaaaaa"))
    with pytest.raises(CodeCheckError, match="not claimed"):
        release(shadow, "aaaaaaaaaaaa", "why", NOW, nonce(shadow, "aaaaaaaaaaaa"))


def test_rejects_a_bad_run_id_and_a_bad_pr_url(shadow):
    make_run(shadow, "aaaaaaaaaaaa")
    claim_next(shadow, NOW)
    with pytest.raises(CodeCheckError, match="invalid run id"):
        finish(shadow, "../escape", "https://github.com/o/r/pull/9", NOW, nonce(shadow, "../escape"))
    with pytest.raises(CodeCheckError, match="pull request URL"):
        finish(shadow, "aaaaaaaaaaaa", "--evil", NOW, nonce(shadow, "aaaaaaaaaaaa"))


def test_marker_is_private_and_a_symlinked_folder_is_refused(shadow, tmp_path):
    make_run(shadow, "aaaaaaaaaaaa")
    claim_next(shadow, NOW)
    path = shadow.root / "aaaaaaaaaaaa.handoff" / "analysis.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    make_run(shadow, "bbbbbbbbbbbb", ts=2)
    real = shadow.root / "bbbbbbbbbbbb.handoff"
    elsewhere = tmp_path / "elsewhere"
    real.rename(elsewhere)
    os.symlink(elsewhere, real)
    out = claim_next(shadow, NOW + 1)
    assert out["run"] is None and out["skipped"].get("no_handoff") == 1
    assert not (elsewhere / "analysis.json").exists()


def test_a_held_lock_reports_busy(shadow):
    make_run(shadow, "aaaaaaaaaaaa")
    shadow.root.mkdir(parents=True, exist_ok=True)
    lock = FileLock(shadow.root / "queue.lock")
    assert lock.acquire()
    try:
        assert claim_next(shadow, NOW) == {"run": None, "skipped": {}, "busy": True}
    finally:
        lock.release()


# -- the diff guard ------------------------------------------------------------------------------------------


def diff(path: str, added=("x",), removed=(), *, status="M", old: str | None = None) -> str:
    head = f"diff --git a/{old or path} b/{path}\n"
    if status == "A":
        head += f"new file mode 100644\n--- /dev/null\n+++ b/{path}\n"
    elif status == "D":
        head += f"deleted file mode 100644\n--- a/{path}\n+++ /dev/null\n"
    elif status == "R":
        head += f"similarity index 90%\nrename from {old}\nrename to {path}\n--- a/{old}\n+++ b/{path}\n"
    else:
        head += f"--- a/{path}\n+++ b/{path}\n"
    body = "@@ -1,1 +1,1 @@\n" + "".join(f"-{r}\n" for r in removed) + "".join(f"+{a}\n" for a in added)
    return head + body


def check(text: str) -> list[str]:
    return violations(summarize_diff(text))


def test_summary_counts_lines_status_and_renames():
    text = (diff("src/codecheck/a.py", ("1", "2"), ("0",)) + diff("tests/new.py", ("t",), status="A")
            + diff("src/codecheck/old.py", (), ("z",), status="D")
            + diff("src/codecheck/n.py", ("q",), status="R", old="src/codecheck/m.py"))
    by_path = {c.path: c for c in summarize_diff(text)}
    assert (by_path["src/codecheck/a.py"].added, by_path["src/codecheck/a.py"].removed) == (2, 1)
    assert by_path["tests/new.py"].status == "A"
    assert by_path["src/codecheck/old.py"].status == "D"
    renamed = by_path["src/codecheck/n.py"]
    assert renamed.status == "R" and renamed.old_path == "src/codecheck/m.py"


@pytest.mark.parametrize("path", [
    "agents/skeptic.md", "src/codecheck/agentgen.py", "src/codecheck/shadow/handoff.py", "tests/test_handoff.py",
    "skills/review-pr/SKILL.md", "docs/handoffs/x.md", "README.md",
])
def test_allowed_paths_pass(path):
    assert check(diff(path)) == []


@pytest.mark.parametrize("path", [
    ".github/workflows/ci.yml", ".githooks/pre-commit", ".claude/settings.json", ".claude/skills/handoff-round/SKILL.md",
    "scripts/gates.sh", "scripts/gate-paths.txt", "hooks/ensure-runtime.sh", "bin/code-check", "installer/cli.js",
    "CLAUDE.md", "LICENSE", "src/codecheck/shadow/queue.py", "src/codecheck/commands/handoff_queue.py",
    "tests/test_handoff_queue.py", "unknown/file.txt",
])
def test_protected_and_unlisted_paths_are_blocked(path):
    out = check(diff(path))
    assert out and path in out[0]


def test_a_rename_into_or_out_of_a_protected_path_is_blocked():
    assert check(diff("src/codecheck/ok.py", status="R", old=".github/workflows/ci.yml"))
    assert check(diff(".github/workflows/ci.yml", status="R", old="src/codecheck/ok.py"))


@pytest.mark.parametrize("path", ["../outside.py", "/etc/passwd", "src/../.github/x.yml"])
def test_path_traversal_is_blocked(path):
    assert check(diff(path))


def test_version_files_may_only_change_their_version_line():
    ok = diff("pyproject.toml", ('version = "0.1.5"',), ('version = "0.1.4"',))
    assert check(ok) == []
    assert check(diff(".claude-plugin/plugin.json", ('  "version": "0.1.5",',), ('  "version": "0.1.4",',))) == []
    assert check(diff("pyproject.toml", ('dependencies = ["evil"]',)))
    assert check(diff("pyproject.toml", ('version = "0.1.5"', 'dependencies = ["evil"]'), ('version = "0.1.4"',)))


def bump(path, fmt, old="0.1.4", new="0.1.5"):
    return diff(path, (fmt.format(new),), (fmt.format(old),))


BUMPS = [
    ("VERSION", "{}"), ("pyproject.toml", 'version = "{}"'), ("package.json", '  "version": "{}",'),
    ("package-lock.json", '  "version": "{}",'), (".claude-plugin/plugin.json", '  "version": "{}",'),
    (".claude-plugin/marketplace.json", '      "version": "{}",'), ("src/codecheck/__init__.py", '__version__ = "{}"'),
    ("tests/test_smoke.py", '    assert __version__ == "{}"'),
]


@pytest.mark.parametrize("path,fmt", BUMPS)
def test_a_one_step_patch_bump_is_allowed_in_every_version_file(path, fmt):
    assert check(bump(path, fmt)) == []


@pytest.mark.parametrize("new", ["0.2.0", "0.1.6", "1.0.0", "0.1.4", "0.1.3"])
def test_only_a_one_step_patch_bump_is_allowed(new):
    assert check(bump("VERSION", "{}", new=new))


def test_a_bump_may_not_hide_other_changes_on_the_same_line():
    mixed = diff("package.json", ('  "version": "0.1.5",', '  "scripts": {"x": "evil"},'), ('  "version": "0.1.4",',))
    assert check(mixed)
    renamed = diff("package.json", ('  "main": "0.1.5",',), ('  "version": "0.1.4",',))
    assert check(renamed)


@pytest.mark.parametrize("path", ["VERSION", "package.json", "package-lock.json", ".claude-plugin/plugin.json"])
def test_other_edits_to_strict_version_files_are_blocked(path):
    assert check(diff(path, ("x",), ("y",)))


@pytest.mark.parametrize("path", ["src/codecheck/__init__.py", "tests/test_smoke.py"])
def test_other_edits_to_soft_version_files_follow_the_normal_rules(path):
    assert check(diff(path, ("x",), ("y",))) == []


def test_existing_golden_cases_may_not_change_but_new_ones_may_be_added():
    assert check(diff("tests/golden/o-r-1-abcd1234.json", status="A")) == []
    assert check(diff("tests/golden/o-r-1-abcd1234.json"))
    assert check(diff("tests/golden/o-r-1-abcd1234.json", (), ("x",), status="D"))


def test_deleting_a_test_file_or_a_test_function_is_blocked():
    assert check(diff("tests/test_x.py", (), ("def test_a(): pass",), status="D"))
    assert check(diff("tests/test_x.py", (), ("def test_a():", "    assert 1")))  # removes a test, adds none
    assert check(diff("tests/test_x.py", ("def test_b():",), ("def test_a():",))) == []
    assert check(diff("tests/test_x.py", ("def test_a():", "def test_b():"), ("def test_a():",))) == []


def test_snippets_may_be_tuned_but_not_deleted():
    assert check(diff("tests/snippets/off-by-one/case.json")) == []
    assert check(diff("tests/snippets/off-by-one/case.json", (), ("x",), status="D"))


def test_a_large_diff_is_blocked():
    big = diff("src/codecheck/a.py", tuple("x" for _ in range(MAX_DIFF_LINES + 1)))
    assert any("lines" in v for v in check(big))
    assert check(diff("src/codecheck/a.py", tuple("x" for _ in range(MAX_DIFF_LINES)))) == []


# -- the CLI -------------------------------------------------------------------------------------------------


def cli_ctx(tmp_path):
    _origin, clone = make_origin_and_clone(tmp_path)
    return make_ctx(tmp_path, clone), clone


def test_cli_next_done_release_round_trip(tmp_path):
    ctx, _ = cli_ctx(tmp_path)
    make_run(ctx.shadow, "aaaaaaaaaaaa", pr="o/r#7")
    code, out = call(ctx, "handoff-queue", "next")
    assert code == 0 and out["run"] == "aaaaaaaaaaaa" and out["pr"] == "o/r#7"
    assert out["handoff_dir"].endswith("aaaaaaaaaaaa.handoff")
    claim = out["claim"]
    _code, bad = call(ctx, "handoff-queue", "release", "--run", "aaaaaaaaaaaa", "--claim", "wrong", "--reason", "x")
    assert "not claimed" in json.dumps(bad)
    _code, out = call(ctx, "handoff-queue", "release", "--run", "aaaaaaaaaaaa", "--claim", claim, "--reason", "tests failed")
    assert out == {"released": True, "run": "aaaaaaaaaaaa"}
    claim = call(ctx, "handoff-queue", "next")[1]["claim"]
    done = ("handoff-queue", "done", "--run", "aaaaaaaaaaaa", "--claim", claim, "--pr", "https://github.com/o/r/pull/3")
    ctx.provider.add_pr(number=3, state="open")
    assert "not merged" in json.dumps(call(ctx, *done)[1])
    ctx.provider.add_pr(number=3, state="merged")
    assert call(ctx, *done)[1] == {"done": True, "run": "aaaaaaaaaaaa"}


def test_cli_next_is_empty_when_the_shadow_store_is_off(tmp_path):
    ctx, _ = cli_ctx(tmp_path)
    ctx.shadow = None
    assert call(ctx, "handoff-queue", "next")[1] == {"run": None, "skipped": {}, "reason": "shadow store is off"}


def test_handoff_queue_is_hidden_from_help_but_parses():
    parser = build_parser()
    text = parser.format_help()
    assert "handoff-queue" not in text and "SUPPRESS" not in text
    assert parser.parse_args(["handoff-queue", "next"]).command == "handoff-queue"


def branch_with(tmp_path, files: dict[str, str], *, state="open", base_ref="main", sha_override=None):
    ctx, clone = cli_ctx(tmp_path)
    sh(clone, "checkout", "-q", "-b", "round")
    for name, text in files.items():
        (clone / name).parent.mkdir(parents=True, exist_ok=True)
        (clone / name).write_text(text)
        sh(clone, "add", name)
    sh(clone, "commit", "-q", "-m", "round")
    head = sh(clone, "rev-parse", "HEAD").strip()
    ctx.provider.add_pr(number=3, head_sha=sha_override or head, base_ref=base_ref, state=state)
    return ctx


def claimed(ctx, run="aaaaaaaaaaaa") -> str:
    make_run(ctx.shadow, run)
    return call(ctx, "handoff-queue", "next")[1]["claim"]


def run_check(ctx, claim, *extra):
    return call(ctx, "handoff-queue", "check", "--run", "aaaaaaaaaaaa", "--claim", claim, "--pr", "3", *extra)


def test_cli_check_passes_for_an_allowed_diff_and_reports_the_sha_to_merge(tmp_path):
    ctx = branch_with(tmp_path, {"tests/test_new.py": "def test_a(): pass\n"})
    code, out = run_check(ctx, claimed(ctx))
    assert code == 0 and out["ok"] is True and out["violations"] == [] and out["replay_passed"] is True
    assert out["head_sha"] == ctx.provider.prs[ctx.resolve_ref("3")].head_sha


def test_cli_check_blocks_a_protected_path(tmp_path):
    ctx = branch_with(tmp_path, {".github/workflows/x.yml": "on: push\n"})
    _code, out = run_check(ctx, claimed(ctx))
    assert out["ok"] is False and any(".github/workflows/x.yml" in v for v in out["violations"])


def test_cli_check_sees_every_commit_of_the_pr_not_just_the_last(tmp_path):
    ctx = branch_with(tmp_path, {".github/workflows/x.yml": "on: push\n"})
    clone = ctx.cwd
    (clone / "tests").mkdir(exist_ok=True)
    (clone / "tests/test_ok.py").write_text("def test_b(): pass\n")
    sh(clone, "add", "tests/test_ok.py")
    sh(clone, "commit", "-q", "-m", "innocent last commit")
    ctx.provider.add_pr(number=3, head_sha=sh(clone, "rev-parse", "HEAD").strip(), base_ref="main")
    _code, out = run_check(ctx, claimed(ctx))
    assert out["ok"] is False and any(".github" in v for v in out["violations"])


def test_cli_check_needs_the_claim_token(tmp_path):
    ctx = branch_with(tmp_path, {"tests/test_new.py": "def test_a(): pass\n"})
    claimed(ctx)
    code, out = run_check(ctx, "not-the-token")
    assert code != 0 and "not claimed" in json.dumps(out)


def test_cli_check_fails_when_local_head_is_not_the_pr_head(tmp_path):
    ctx = branch_with(tmp_path, {"tests/test_new.py": "def test_a(): pass\n"}, sha_override="0" * 40)
    _code, out = run_check(ctx, claimed(ctx))
    assert out["ok"] is False and any("PR head" in v for v in out["violations"])


@pytest.mark.parametrize("kwargs,word", [({"state": "closed"}, "open"), ({"base_ref": "other"}, "base")])
def test_cli_check_requires_an_open_pr_on_the_default_branch(tmp_path, kwargs, word):
    ctx = branch_with(tmp_path, {"tests/test_new.py": "def test_a(): pass\n"}, **kwargs)
    _code, out = run_check(ctx, claimed(ctx))
    assert out["ok"] is False and any(word in v for v in out["violations"])


def test_cli_check_fails_when_the_replay_fails(tmp_path):
    ctx = branch_with(tmp_path, {"tests/golden/broken.json": "{not json"})
    _code, out = run_check(ctx, claimed(ctx))
    assert out["ok"] is False and out["replay_passed"] is False


def test_cli_check_has_no_user_chosen_base(tmp_path):
    ctx = branch_with(tmp_path, {"tests/test_new.py": "x = 1\n"})
    claim = claimed(ctx)
    with pytest.raises(SystemExit):
        run_check(ctx, claim, "--base", "HEAD~1")


# -- bypasses found in review --------------------------------------------------------------------------------


def test_a_rename_out_of_an_unlisted_or_strict_path_is_blocked():
    assert check(diff("docs/p.txt", (), (), status="R", old="pyproject.toml"))
    assert check(diff("docs/p.txt", (), (), status="R", old=".gitignore"))
    assert check(diff("docs/p.txt", (), (), status="R", old="package.json"))


@pytest.mark.parametrize("old,new", [("tests/test_a.py", "tests/a.txt"), ("tests/test_a.py", "docs/a.txt"),
                                     ("docs/a.txt", "tests/test_a.py")])
def test_any_rename_touching_tests_is_blocked(old, new):
    assert check(diff(new, (), (), status="R", old=old))


def test_quoted_and_odd_paths_fail_closed():
    quoted = ('diff --git "a/.github/\\303\\251.yml" b/src/codecheck/x.yml\nsimilarity index 100%\n'
              'rename from ".github/\\303\\251.yml"\nrename to src/codecheck/x.yml\n')
    assert check(quoted)
    for path in ["src/codecheck/a b.py", "src/codecheck/\u00e9.py", "src/codecheck/a\\b.py", "src/codecheck/a\tb.py"]:
        assert check(diff(path)), path


@pytest.mark.parametrize("path", [
    "src/codecheck/shadow/Queue.py", "src/codecheck/commands/Handoff_queue.py", "tests/Test_handoff_queue.py",
    "claude.MD", "Tests/conftest.py",
])
def test_protected_paths_are_matched_case_insensitively(path):
    assert check(diff(path, status="A"))


@pytest.mark.parametrize("path", [
    "src/codecheck/core/state.py", "src/codecheck/shadow/store.py", "src/codecheck/shadow/scrub.py",
    "src/codecheck/evals/golden.py", "src/codecheck/vcs/git.py", "src/codecheck/runids.py", "src/codecheck/errors.py",
    "src/codecheck/runner.py", "src/codecheck/cli.py", "src/codecheck/commands/__init__.py",
    "src/codecheck/config.py", "src/codecheck/commands/run_pr.py", "src/codecheck/commands/loop.py",
    "src/codecheck/core/policy.py", "src/codecheck/providers/github.py", "tests/conftest.py", "tests/__init__.py",
    "src/codecheck/conftest.py", "docs/decisions/0099-x.md", "docs/spikes/x.md",
])
def test_the_guards_dependencies_and_security_plumbing_are_protected(path):
    out = check(diff(path))
    assert out and path in out[0]


@pytest.mark.parametrize("line", [
    "pytestmark = pytest.mark.skip", "@pytest.mark.skipif(True, reason='x')", "@pytest.mark.xfail",
    "collect_ignore_glob = ['*']", "def pytest_sessionfinish(session, exitstatus):", "pytest.importorskip('x')",
])
def test_adding_lines_that_switch_tests_off_is_blocked(line):
    assert check(diff("tests/test_x.py", (line,)))
    assert check(diff("src/codecheck/x.py", (line,))) == []  # only test code is held to this


@pytest.mark.parametrize("header", [
    "new file mode 120000", "new file mode 100755", "old mode 100644\nnew mode 100755", "new file mode 160000",
])
def test_symlinks_gitlinks_and_mode_changes_are_blocked(header):
    text = f"diff --git a/src/codecheck/x.py b/src/codecheck/x.py\n{header}\n--- a/src/codecheck/x.py\n+++ b/src/codecheck/x.py\n@@ -1 +1 @@\n+x\n"
    assert check(text)


def test_binary_files_are_blocked_and_cost_a_line_each():
    text = "diff --git a/docs/a.png b/docs/a.png\nnew file mode 100644\nBinary files /dev/null and b/docs/a.png differ\n"
    assert any("binary" in v for v in check(text))
    many = "".join(diff(f"docs/f{i}.md", (), (), status="R", old=f"docs/g{i}.md") for i in range(70))
    assert any("files" in v for v in check(many))


@pytest.mark.parametrize("path,old,new", [
    ("pyproject.toml", 'dependencies = ["foo==0.1.4"]', 'dependencies = ["foo==0.1.5"]'),
    ("package.json", '  "foo": "0.1.4",', '  "foo": "0.1.5",'),
    ("VERSION", "0.1.4 # x", "0.1.5 # x"),
])
def test_a_bump_must_match_the_files_own_version_line_shape(path, old, new):
    assert check(diff(path, (new,), (old,)))


def test_a_stale_or_wrong_claim_token_is_refused(shadow):
    make_run(shadow, "aaaaaaaaaaaa")
    first = claim_next(shadow, NOW)
    with pytest.raises(CodeCheckError, match="not claimed"):
        finish(shadow, "aaaaaaaaaaaa", "https://github.com/o/r/pull/9", NOW, "wrong")
    with pytest.raises(CodeCheckError, match="not claimed"):
        release(shadow, "aaaaaaaaaaaa", "x", NOW, "")
    retaken = claim_next(shadow, NOW + CLAIM_STALE_SECONDS + 1)
    assert retaken["claim"] != first["claim"]
    with pytest.raises(CodeCheckError, match="not claimed"):
        finish(shadow, "aaaaaaaaaaaa", "https://github.com/o/r/pull/9", NOW, first["claim"])


def test_a_corrupt_claim_time_does_not_abort_the_queue(shadow):
    make_run(shadow, "aaaaaaaaaaaa", ts=1)
    make_run(shadow, "bbbbbbbbbbbb", ts=2)
    claim_next(shadow, NOW)
    path = shadow.root / "aaaaaaaaaaaa.handoff" / "analysis.json"
    path.write_text(json.dumps({"status": "claimed", "attempts": 1, "claimed_ts": "not a number"}))
    assert claim_next(shadow, NOW + 1)["run"] in ("aaaaaaaaaaaa", "bbbbbbbbbbbb")
