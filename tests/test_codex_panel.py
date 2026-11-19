import io
import json
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Lock

import pytest
from clihelp import call as cli_call
from fakes import FakeProvider, FakeRunner

from codecheck.cli import build_context, main
from codecheck.commands import panel
from codecheck.config import Config, load_config, model_for
from codecheck.errors import ConfigError

MEMBERS = ["architect", "pragmatist", "skeptic", "lang-python"]
RUN_ID = "123456789abc"


@pytest.fixture(autouse=True)
def measured_platform(monkeypatch):
    monkeypatch.setattr(panel, "platform", "darwin", raising=False)


class CaptureRunner(FakeRunner):
    def __init__(self, responses=None, gate=None):
        super().__init__()
        self.on_prefix("git", returncode=1)
        self.on_prefix("codex", "exec")
        self.responses = responses or {}
        self.gate = gate
        self.lock = Lock()
        self.active = 0
        self.peak = 0

    def run(self, args, **kwargs):
        result = super().run(args, **kwargs)
        if args[0] != "codex":
            return result
        capture = Path(args[args.index("--output-last-message") + 1])
        name = capture.stem if capture.stem == "aggregator" else capture.stem.rsplit("-", 1)[0]
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if self.gate and name != "aggregator":
                self.gate.wait(timeout=3)
            if name == "aggregator":
                assert (capture.parents[1] / "ingest.json").exists()
            default = '{"findings":[],"dropped":[]}' if name == "aggregator" else '{"findings":[]}'
            code, raw = self.responses.get(capture.stem, self.responses.get(name, (0, default)))
            capture.write_text(raw)
            return replace(result, returncode=code)
        finally:
            with self.lock:
                self.active -= 1


def prepare(tmp_path, runner, **env):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    config_env = {"CODE_CHECK_STATE_DIR": str(tmp_path / "state"), "CODE_CHECK_SHADOW__ENABLED": "false", **env}
    ctx = build_context(checkout, config_env, tmp_path, runner, FakeProvider())
    run_dir = ctx.run_dir(RUN_ID)
    (run_dir / "findings").mkdir(parents=True)
    (run_dir / "context.json").write_text(json.dumps({
        "root": str(checkout), "panel": {"members": MEMBERS, "languages": {"python": ["sample.py"]}},
        "files": [{"path": "sample.py"}], "chunks": [["sample.py"]],
    }))
    return checkout, ctx, run_dir


def execute(ctx):
    out = io.StringIO()
    exit_code = main(["panel", "run", "--run", RUN_ID], ctx=ctx, out=out)
    return exit_code, json.loads(out.getvalue())


def prepare_pr(tmp_path, runner):
    _, ctx, run_dir = prepare(tmp_path, runner, CODE_CHECK_REVIEW__AUTO_APPROVE="true")
    pr = ctx.provider.add_pr()
    meta_path = run_dir / "context.json"
    meta = json.loads(meta_path.read_text())
    meta.update(mode="pr", pr=str(pr.ref), head_sha=pr.head_sha, run_id=RUN_ID)
    meta_path.write_text(json.dumps(meta))
    (run_dir / "diff.patch").write_text(
        "diff --git a/sample.py b/sample.py\n--- a/sample.py\n+++ b/sample.py\n"
        "@@ -3 +3 @@\n-old_value = 1\n+new_value = 2\n"
    )
    return ctx, run_dir, pr


def test_measured_argv_and_valid_findings(tmp_path):
    finding = {"file": "sample.py", "line": 3, "severity": "warning", "category": "correctness",
               "message": "Incorrect result at the boundary.", "suggestion": None}
    runner = CaptureRunner({"lang-python": (0, json.dumps({"findings": [finding]}))})
    checkout, ctx, run_dir = prepare(tmp_path, runner)
    meta_path = run_dir / "context.json"
    meta = json.loads(meta_path.read_text())
    option_like_path = "--dangerously-bypass-approvals-and-sandbox"
    meta["files"].append({"path": option_like_path})
    meta["chunks"][0].append(option_like_path)
    meta_path.write_text(json.dumps(meta))
    code, result = execute(ctx)
    assert code == 0 and result["failed_members"] == {}
    calls = [c for c in runner.calls if c.argv[0] == "codex"]
    assert len(calls) == 5
    for call in calls:
        capture = Path(call.argv[call.argv.index("--output-last-message") + 1])
        assert capture.is_relative_to(run_dir)
        name = capture.stem.removesuffix("-1")
        model = {"aggregator": "gpt-6.2-astra", "lang-python": "gpt-6-luna"}.get(name, "gpt-6.2-sol")
        effort = "high" if name in ("aggregator", "lang-python") else "medium"
        assert call.argv[:-1] == (
            "codex", "exec", "-s", "read-only", "-C", str(checkout), "--ephemeral",
            "--ignore-user-config", "--disable", "hooks", f"--model={model}", "-c",
            'approval_policy="never"', "-c", f'model_reasoning_effort="{effort}"',
            "-c", 'shell_environment_policy.inherit="core"',
            "-c", 'shell_environment_policy.include_only=["HOME","PATH","TMPDIR"]',
            "--output-schema", str(capture.parent / ("aggregator-schema.json" if name == "aggregator" else "reviewer-schema.json")),
            "--output-last-message", str(capture), "--json", "--",
        )
        assert call.input == ""
        assert "do not write any file" in call.argv[-1].lower()
        assert str(run_dir) in call.argv[-1]
        if name == "architect":
            assert option_like_path in call.argv[-1]
            assert option_like_path not in call.argv
    assert json.loads((run_dir / "findings/lang-python.json").read_text())[0]["line"] == 3


def test_unmeasured_platform_refuses_dispatch_without_user_config_override(tmp_path, monkeypatch):
    monkeypatch.setattr(panel, "platform", "linux", raising=False)
    runner = CaptureRunner()
    _, ctx, _ = prepare(tmp_path, runner)
    code, result = execute(ctx)
    assert code == 1 and "codex.allow_unmeasured_platform" in result["error"]
    assert all(call.argv[0] != "codex" for call in runner.calls)
    user_config = tmp_path / ".config/code-check"
    user_config.mkdir(parents=True)
    (user_config / "config.toml").write_text("[codex]\nallow_unmeasured_platform = true\n")
    ctx.config = load_config(ctx.cwd, tmp_path, ctx.env)
    assert execute(ctx)[1]["failed_members"] == {}
    assert sum(call.argv[0] == "codex" for call in runner.calls) == 5


def test_unmeasured_platform_override_cannot_come_from_repo(tmp_path):
    (tmp_path / ".code-check.toml").write_text("[codex]\nallow_unmeasured_platform = true\n")
    with pytest.raises(ConfigError, match="security-sensitive"):
        load_config(tmp_path, tmp_path, {})


def test_failed_codex_reviewer_posts_incomplete_comment(tmp_path):
    runner = CaptureRunner({"architect": (2, "")})
    ctx, run_dir, pr = prepare_pr(tmp_path, runner)
    code, result = execute(ctx)
    assert code == 0 and result["failed_members"] == {"architect": "Codex exit status 2"}
    ingest = json.loads((run_dir / "ingest.json").read_text())
    assert ingest["incomplete"] and ingest["missing"] == ["architect"]
    assert "aggregator" in {Path(f).stem for f in result["files_written"]}
    code, result = cli_call(ctx, "post", "--run", RUN_ID)
    assert code == 0 and result["posted"] and result["incomplete"]
    assert result["decision"] == "COMMENT" and result["failed_reviewers"] == ["architect"]
    (posted,) = ctx.provider.posted
    assert posted["decision"].event == "COMMENT" and "architect" in posted["body"]
    assert ctx.state.get(f"review-incomplete:{pr.ref}") == pr.head_sha
    assert ctx.state.get(f"review:{pr.ref}") is None


def test_codex_capture_redacts_auth_tokens_before_posting(tmp_path):
    jwt = "eyJmaXh0dXJlIjp0cnVlfQ.eyJzdWIiOiJ0ZXN0In0.syntheticSignature"
    opaque = "fixture-opaque-auth-value"
    finding = {
        "file": "sample.py", "line": 3, "severity": "warning", "category": "security",
        "message": f"Credentials in output: {jwt} and token={opaque}",
        "suggestion": f"token={opaque}",
    }
    runner = CaptureRunner({
        "architect": (0, json.dumps({"findings": [finding]})),
        "aggregator": (0, json.dumps({"findings": [{**finding, "reviewers": ["architect"]}], "dropped": []})),
    })
    ctx, run_dir, _ = prepare_pr(tmp_path, runner)
    assert execute(ctx)[1]["failed_members"] == {}
    (clean,) = json.loads((run_dir / "findings/architect.json").read_text())
    assert "[REDACTED] and token=[REDACTED]" in clean["message"]
    assert clean.get("suggestion") is None
    code, result = cli_call(ctx, "post", "--run", RUN_ID)
    assert code == 0 and result["inline"] == 1
    (posted,) = ctx.provider.posted
    (comment,) = posted["comments"]
    assert "[REDACTED] and token=[REDACTED]" in comment.body
    for secret in (jwt, opaque):
        assert secret not in clean["message"] and secret not in comment.body


def test_failed_members_never_look_clean(tmp_path):
    runner = CaptureRunner({"architect": (2, '{"findings":[]}'), "pragmatist": (124, '{"findings":[]}'),
                            "skeptic": (0, "broken"), "lang-python": (0, '{"findings":[{}]}')})
    _, ctx, run_dir = prepare(tmp_path, runner)
    for member in MEMBERS:
        (run_dir / "findings" / f"{member}.json").write_text("[]")
    code, result = execute(ctx)
    assert code == 0
    assert set(result["failed_members"]) == {*MEMBERS, "aggregator"}
    for member, reason in zip(MEMBERS, ("exit", "timed out", "JSON", "schema"), strict=True):
        assert reason in result["failed_members"][member]
        assert not (run_dir / "findings" / f"{member}.json").exists()
    assert result["failed_members"]["aggregator"] == "no usable reviewer output"
    assert sum(call.argv[0] == "codex" for call in runner.calls) == len(MEMBERS)
    assert result["files_written"] == []
    assert not (run_dir / "findings" / "aggregator.json").exists()


def test_parallel_cap(tmp_path):
    runner = CaptureRunner(gate=Barrier(2))
    _, ctx, _ = prepare(tmp_path, runner, CODE_CHECK_CODEX__MAX_PARALLEL="2")
    assert execute(ctx)[1]["failed_members"] == {}
    assert runner.peak == 2


def test_chunk_scoping_and_failed_chunk_invalidate_the_member(tmp_path):
    runner = CaptureRunner({"architect-2": (2, "")})
    _, ctx, run_dir = prepare(tmp_path, runner)
    meta_path = run_dir / "context.json"
    meta = json.loads(meta_path.read_text())
    meta["files"].append({"path": "readme.md"})
    meta["chunks"].append(["readme.md"])
    meta_path.write_text(json.dumps(meta))
    code, result = execute(ctx)
    assert code == 0 and set(result["failed_members"]) == {"architect"}
    calls = {
        Path(call.argv[call.argv.index("--output-last-message") + 1]).stem: call.argv[-1]
        for call in runner.calls if call.argv[0] == "codex"
    }
    assert "lang-python-1" in calls and "lang-python-2" not in calls
    assert 'Files you own (JSON): ["readme.md"]' in calls["architect-2"]
    assert set(json.loads((run_dir / "ingest.json").read_text())["missing"]) == {"architect"}
    assert not (run_dir / "findings/architect.json").exists()


def test_bad_aggregator_capture_removes_stale_output(tmp_path):
    runner = CaptureRunner({"aggregator": (0, '{"findings":[],"dropped":[{}]}')})
    _, ctx, run_dir = prepare(tmp_path, runner)
    (run_dir / "findings/aggregator.json").write_text('{"findings":[],"dropped":[]}')
    code, result = execute(ctx)
    assert code == 0 and "schema" in result["failed_members"]["aggregator"]
    assert not (run_dir / "findings/aggregator.json").exists()


def test_invalid_model_refused_before_dispatch(tmp_path):
    runner = CaptureRunner()
    _, ctx, _ = prepare(tmp_path, runner)
    ctx.config = replace(ctx.config, codex_models=replace(ctx.config.codex_models, reviewers="--bad"))
    code, result = execute(ctx)
    assert code == 1 and "model" in result["error"]
    assert all(call.argv[0] != "codex" for call in runner.calls)


@pytest.mark.parametrize("table", ["codex", "codex_models", "codex_reasoning"])
def test_codex_tables_are_user_only(tmp_path, table):
    (tmp_path / ".code-check.toml").write_text(f"[{table}]\n")
    with pytest.raises(ConfigError, match="security-sensitive"):
        load_config(tmp_path, tmp_path, {})


def test_model_selection_and_user_override(tmp_path):
    user = tmp_path / ".config/code-check"
    user.mkdir(parents=True)
    (user / "config.toml").write_text('[codex_models.per_language]\npython = "gpt-custom"\n')
    cfg = load_config(tmp_path, tmp_path, {})
    assert model_for(cfg, "language_expert", "python", runtime="codex") == "gpt-custom"
    assert model_for(Config(), "language_expert", "python", runtime="codex") == "gpt-6-luna"
    assert model_for(cfg.models, "language_expert", "python") == "haiku"
    assert model_for(cfg, "judge", runtime="codex") == "gpt-6.2-astra"
    with pytest.raises(ConfigError, match="unknown model role"):
        model_for(cfg.models, "judge")


@pytest.mark.parametrize("effort", ["xhigh", "max", "ultra"])
def test_unmeasured_reasoning_effort_is_rejected(tmp_path, effort):
    with pytest.raises(ConfigError, match="must be one of low, medium, high"):
        load_config(tmp_path, tmp_path, {"CODE_CHECK_CODEX_REASONING__REVIEWERS": effort})
