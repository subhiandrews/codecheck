import json
import re
import subprocess
from pathlib import Path

import pytest
from clihelp import call, make_ctx
from fakes import FakeRunner, HybridRunner

from codecheck.errors import CodeCheckError
from codecheck.evals.live import (
    DEFAULT_SNIPPETS,
    REVIEW_PROMPT,
    claude_reachable,
    load_snippets,
    plugin_root,
    run_snippet,
)
from codecheck.shadow.store import ShadowStore

REPO = Path(__file__).resolve().parent.parent
PROBE = ("claude", "-p", "reply with ok", "--output-format", "json", "--permission-mode", "dontAsk")
OK = json.dumps({"type": "result", "is_error": False, "result": "ok", "total_cost_usd": 0.01})
SNIPPET = {
    "name": "tiny-bug",
    "base": {"pkg/calc.py": "def one():\n    return 1\n"},
    "change": {"pkg/calc.py": "def one():\n    return 2\n", "pkg/new_mod.py": "X = 1\n"},
    "expected": [{"file": "pkg/calc.py", "line": 2, "category": ["bug", "correctness"], "severity": "critical"}],
    "must_not_report": [],
}
FOUND = {"file": "pkg/calc.py", "line": 2, "severity": "critical", "category": "bug", "message": "returns 2, not 1"}


def _write_snippet(directory: Path, snippet: dict) -> None:
    folder = directory / snippet["name"]
    folder.mkdir(parents=True)
    (folder / "case.json").write_text(json.dumps(snippet))


def _seed_shadow(store_dir: Path, run_id: str = "abcdef123456", aggregator=None, root: str | None = None) -> None:
    """What the review-local skill's hand-off step leaves in the shadow store."""
    store = ShadowStore(store_dir)
    store.append(run_id, {"type": "run", "skill": "review-local"})
    data = {"reviewers": {}, "root": root}
    if aggregator is not None:
        data["aggregator"] = aggregator
    store.save_raw(run_id, data)


class ReviewingRunner(HybridRunner):
    """Real git; a fake `claude` that, for a review, leaves hand-off output where the env assignments point."""

    def __init__(self, fake: FakeRunner, findings: list[dict]) -> None:
        super().__init__(fake)
        self.findings = findings

    def run(self, args, *, input=None, cwd=None, timeout=None):
        if args and args[0] == "env":
            shadow = next(a.split("=", 1)[1] for a in args if a.startswith("CODE_CHECK_SHADOW__STORE="))
            _seed_shadow(Path(shadow), aggregator={"findings": self.findings, "dropped": []})
        return super().run(args, input=input, cwd=cwd, timeout=timeout)


def _ctx(tmp_path, runner, env=None):
    ctx = make_ctx(tmp_path, tmp_path, runner=runner)
    ctx.env = env or {}
    return ctx


# --- reachability ------------------------------------------------------------------------------------------------


def test_claude_reachable_needs_parseable_non_error_json(tmp_path):
    assert claude_reachable(_ctx(tmp_path, FakeRunner().on(*PROBE, stdout=OK))) is True
    err = json.dumps({"is_error": True, "result": "Invalid API key"})
    assert claude_reachable(_ctx(tmp_path, FakeRunner().on(*PROBE, stdout=err))) is False
    assert claude_reachable(_ctx(tmp_path, FakeRunner().on(*PROBE, stdout="nope", returncode=1))) is False
    runner = FakeRunner().on(*PROBE, stdout=OK)
    claude_reachable(_ctx(tmp_path, runner))
    assert runner.calls[0].input == ""  # stdin closed


def test_live_unreachable_raises_without_flag(tmp_path):
    runner = FakeRunner().on(*PROBE, returncode=127, stderr="claude: not found")
    code, out = call(_ctx(tmp_path, runner), "eval", "--live")
    assert code == 1 and out["error"].startswith("model not reachable: ")
    assert runner.argvs() == [PROBE]  # nothing else ran


def test_live_unreachable_skips_with_flag(tmp_path):
    runner = FakeRunner().on(*PROBE, returncode=127, stderr="claude: not found")
    code, out = call(_ctx(tmp_path, runner), "eval", "--live", "--if-reachable")
    assert code == 0 and out["passed"] is True and out["cases"] == [] and out["skipped"]
    assert runner.argvs() == [PROBE]


def test_replay_and_live_are_mutually_exclusive(tmp_path):
    with pytest.raises(SystemExit):
        call(_ctx(tmp_path, FakeRunner()), "eval", "--replay", "--live")


# --- snippets ----------------------------------------------------------------------------------------------------


def test_load_snippets_names_bad_file(tmp_path):
    _write_snippet(tmp_path, SNIPPET)
    bad = tmp_path / "broken"
    bad.mkdir()
    (bad / "case.json").write_text("{not json")
    with pytest.raises(CodeCheckError, match=r"broken[/\\]case\.json"):
        load_snippets(tmp_path)
    (bad / "case.json").write_text(json.dumps({**SNIPPET, "name": "broken", "change": {"../escape.py": "x"}}))
    with pytest.raises(CodeCheckError, match=r"broken[/\\]case\.json.*escape"):
        load_snippets(tmp_path)
    (bad / "case.json").write_text(json.dumps({**SNIPPET, "name": "broken", "expected": [{"file": "a.py"}]}))
    with pytest.raises(CodeCheckError, match=r"broken[/\\]case\.json.*expected"):
        load_snippets(tmp_path)


def test_load_snippets_reads_sorted_cases(tmp_path):
    _write_snippet(tmp_path, {**SNIPPET, "name": "b"})
    _write_snippet(tmp_path, {**SNIPPET, "name": "a"})
    assert [s["name"] for s in load_snippets(tmp_path)] == ["a", "b"]


def test_shipped_snippets_load_and_are_marked_uncalibrated():
    assert DEFAULT_SNIPPETS == REPO / "tests" / "snippets"
    snippets = {s["name"]: s for s in load_snippets(DEFAULT_SNIPPETS)}
    assert set(snippets) == {"off-by-one", "clean-code"}
    assert all(s["calibrated"] is False for s in snippets.values())
    assert snippets["off-by-one"]["expected"] and snippets["clean-code"]["expected"] == []


def test_only_rejects_unknown_snippet(tmp_path):
    snippets = tmp_path / "snips"
    _write_snippet(snippets, SNIPPET)
    runner = FakeRunner().on(*PROBE, stdout=OK)
    code, out = call(_ctx(tmp_path, runner), "eval", "--live", "--snippets", str(snippets), "--only", "nope")
    assert code == 1 and "nope" in out["error"] and "tiny-bug" in out["error"]
    assert runner.argvs() == []  # validated before probing the model


# --- one snippet -------------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("git_env")
def test_run_snippet_builds_isolated_argv(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    fake = FakeRunner().on_prefix("env", stdout=OK)
    _seed_shadow(workdir / "shadow", aggregator={"findings": [FOUND], "dropped": []})
    ctx = _ctx(tmp_path, HybridRunner(fake))

    out = run_snippet(ctx, SNIPPET, workdir)

    [call_] = fake.calls  # git ran for real; the only scripted command is the review
    argv = call_.argv
    w = str(workdir.resolve())
    assert argv[:4] == ("env", f"CODE_CHECK_STATE_DIR={w}/state", f"CODE_CHECK_SHADOW__STORE={w}/shadow",
                        "CODE_CHECK_SHADOW__HANDOFF=1")
    tools = ctx.config.run_pr.review_allowed_tools
    claude = argv[argv.index("claude"):]
    assert claude == ("claude", "-p", REVIEW_PROMPT, "--plugin-dir", str(REPO), "--output-format", "json",
                      "--permission-mode", "dontAsk", "--permission-prompts", "none", "--add-dir", w,
                      "--allowedTools", *tools)
    assert all(a.startswith("CODE_CHECK_") for a in argv[1:argv.index("claude")])
    # Nothing from the snippet reaches argv.
    text = " ".join(argv)
    for needle in ("tiny-bug", "calc", "new_mod", "return"):
        assert needle not in text
    assert call_.input == "" and Path(call_.cwd) == workdir.resolve() / "repo"

    # The clone has the base committed and pushed, and the change as an uncommitted edit.
    repo = workdir / "repo"
    git = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout
    assert git("show", "origin/main:pkg/calc.py") == SNIPPET["base"]["pkg/calc.py"]
    assert (repo / "pkg/calc.py").read_text() == SNIPPET["change"]["pkg/calc.py"]
    assert set(git("status", "--porcelain").split("\n")) - {""} == {" M pkg/calc.py", "?? pkg/new_mod.py"}

    assert out["name"] == "tiny-bug" and out["result"]["passed"] is True and out["result"]["matched"] == 1
    assert out["got"] == [{"file": "pkg/calc.py", "line": 2, "category": "bug", "severity": "critical"}]
    assert out["claude"]["ok"] is True


@pytest.mark.usefixtures("git_env")
def test_run_snippet_uses_claude_plugin_root_and_strips_worktree_root(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    fake = FakeRunner().on_prefix("env", stdout=OK)
    _seed_shadow(workdir / "shadow", root="/wt/x", aggregator=[{**FOUND, "file": "/wt/x/pkg/calc.py"}])
    ctx = _ctx(tmp_path, HybridRunner(fake), env={"CLAUDE_PLUGIN_ROOT": str(tmp_path / "plug")})
    out = run_snippet(ctx, SNIPPET, workdir)
    argv = fake.calls[0].argv
    assert argv[argv.index("--plugin-dir") + 1] == str((tmp_path / "plug").resolve())
    assert out["got"][0]["file"] == "pkg/calc.py" and out["result"]["passed"] is True


def test_plugin_root_default_is_repo_root():
    assert plugin_root({}) == REPO


@pytest.mark.usefixtures("git_env")
def test_run_snippet_without_handoff_output_fails_the_case(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    fake = FakeRunner().on_prefix("env", stdout=OK)
    clean = {**SNIPPET, "expected": []}  # would pass vacuously on no findings
    out = run_snippet(_ctx(tmp_path, HybridRunner(fake)), clean, workdir)
    assert out["result"]["passed"] is False and "no hand-off" in out["error"] and out["got"] == []


@pytest.mark.usefixtures("git_env")
def test_run_snippet_reports_a_failed_claude_run(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    fake = FakeRunner().on_prefix("env", stdout=json.dumps({"is_error": True, "result": "boom"}), returncode=1)
    _seed_shadow(workdir / "shadow", aggregator={"findings": [FOUND]})
    out = run_snippet(_ctx(tmp_path, HybridRunner(fake)), SNIPPET, workdir)
    assert out["result"]["passed"] is False and out["claude"]["ok"] is False and "boom" in out["error"]


# --- end to end through the CLI ----------------------------------------------------------------------------------


@pytest.mark.usefixtures("git_env")
def test_live_cli_runs_each_snippet_once(tmp_path):
    snippets = tmp_path / "snips"
    _write_snippet(snippets, SNIPPET)
    _write_snippet(snippets, {**SNIPPET, "name": "other", "expected": [], "must_not_report": [{"file": "pkg/calc.py"}]})
    fake = FakeRunner().on(*PROBE, stdout=OK).on_prefix("env", stdout=OK)
    ctx = _ctx(tmp_path, ReviewingRunner(fake, [FOUND]))

    code, out = call(ctx, "eval", "--live", "--snippets", str(snippets))
    assert code == 0 and out["passed"] is False  # "other" forbids any finding in calc.py
    by_name = {c["name"]: c for c in out["cases"]}
    assert by_name["tiny-bug"]["result"]["passed"] is True and by_name["other"]["result"]["passed"] is False
    reviews = [a for a in fake.argvs() if a[0] == "env"]
    assert len(reviews) == 2 and fake.argvs()[0] == PROBE
    # Each snippet got its own throwaway directory, removed afterwards.
    dirs = {re.sub(r"/(state|shadow)$", "", a.split("=", 1)[1]) for r in reviews for a in r[1:3]}
    assert len(dirs) == 2 and not any(Path(d).exists() for d in dirs)

    code, out = call(ctx, "eval", "--live", "--snippets", str(snippets), "--only", "tiny-bug")
    assert out["passed"] is True and [c["name"] for c in out["cases"]] == ["tiny-bug"]


def test_mode_specific_flags_are_refused_in_the_other_mode(tmp_path):
    ctx = _ctx(tmp_path, FakeRunner())
    code, out = call(ctx, "eval", "--live", "--cases", str(tmp_path))
    assert code == 1 and "--replay only" in out["error"]
    code, out = call(ctx, "eval", "--replay", "--only", "x")
    assert code == 1 and "--live only" in out["error"]


def test_readme_documents_the_live_eval():
    from codecheck.evals.match import RECALL_BAR, TOLERANCE

    readme = (REPO / "README.md").read_text()
    section = readme.split("### Live snippet eval", 1)[1].split("\n## ", 1)[0]
    for text in ("costs real tokens", "not run in CI", "local only", "--if-reachable", "--only", "--snippets",
                 f"within {TOLERANCE}", f"{round(RECALL_BAR * 100)}%", '"calibrated": false', "CODE_CHECK_STATE_DIR",
                 "CODE_CHECK_SHADOW__STORE", "CODE_CHECK_SHADOW__HANDOFF=1", "case-insensitively"):
        assert text in section, text
