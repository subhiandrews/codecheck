import io
import json
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Lock

import pytest
from fakes import FakeProvider, FakeRunner

from codecheck.cli import build_context, main
from codecheck.config import Config, load_config, model_for
from codecheck.errors import ConfigError

MEMBERS = ["architect", "pragmatist", "skeptic", "lang-python"]
RUN_ID = "123456789abc"


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
    ctx = build_context(checkout, {"CODE_CHECK_STATE_DIR": str(tmp_path / "state"), **env}, tmp_path, runner, FakeProvider())
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


def test_measured_argv_and_valid_findings(tmp_path):
    finding = {"file": "sample.py", "line": 3, "severity": "warning", "category": "correctness",
               "message": "Incorrect result at the boundary.", "suggestion": None}
    runner = CaptureRunner({"lang-python": (0, json.dumps({"findings": [finding]}))})
    checkout, ctx, run_dir = prepare(tmp_path, runner)
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
            "--output-schema", str(capture.parent / ("aggregator-schema.json" if name == "aggregator" else "reviewer-schema.json")),
            "--output-last-message", str(capture), "--json", "--",
        )
        assert call.input == ""
        assert "do not write any file" in call.argv[-1].lower()
        assert str(run_dir) in call.argv[-1]
    assert json.loads((run_dir / "findings/lang-python.json").read_text())[0]["line"] == 3


def test_failed_members_never_look_clean(tmp_path):
    runner = CaptureRunner({"architect": (2, '{"findings":[]}'), "pragmatist": (124, '{"findings":[]}'),
                            "skeptic": (0, "broken"), "lang-python": (0, '{"findings":[{}]}')})
    _, ctx, run_dir = prepare(tmp_path, runner)
    for member in MEMBERS:
        (run_dir / "findings" / f"{member}.json").write_text("[]")
    code, result = execute(ctx)
    assert code == 0
    assert set(result["failed_members"]) == set(MEMBERS)
    for member, reason in zip(MEMBERS, ("exit", "timed out", "JSON", "schema"), strict=True):
        assert reason in result["failed_members"][member]
        assert not (run_dir / "findings" / f"{member}.json").exists()


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
