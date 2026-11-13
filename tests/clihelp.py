import io
import json
from dataclasses import replace
from pathlib import Path

from fakes import FakeProvider
from gitutil import make_origin_and_clone, push_branch

from codecheck.cli import AppContext, main
from codecheck.config import Config, ShadowConfig
from codecheck.core.state import StateStore
from codecheck.runner import SubprocessRunner
from codecheck.shadow.store import ShadowStore
from codecheck.vcs.git import Git


def make_ctx(tmp_path, clone, *, provider=None, runner=None, **config_over) -> AppContext:
    cfg = replace(Config(), state_dir=str(tmp_path / "state"), shadow=ShadowConfig(True, str(tmp_path / "shadow")), **config_over)
    runner = runner or SubprocessRunner()
    return AppContext(
        config=cfg, runner=runner, provider=provider or FakeProvider(), git=Git(runner, clone),
        state=StateStore(tmp_path / "state" / "state.json"), shadow=ShadowStore(tmp_path / "shadow"),
        cwd=Path(clone), env={},
    )


def call(ctx, *argv: str, stdin: str | None = None):
    import sys

    out = io.StringIO()
    old = sys.stdin
    if stdin is not None:
        sys.stdin = io.StringIO(stdin)
    try:
        code = main(list(argv), ctx=ctx, out=out)
    finally:
        sys.stdin = old
    return code, json.loads(out.getvalue())


def write_findings(run_dir, mapping: dict) -> None:
    for member, content in mapping.items():
        (Path(run_dir) / "findings" / f"{member}.json").write_text(json.dumps(content))


def finish_review(ctx, out, per_member=None, aggregated=None):
    """Simulate the agents: every panel member writes a findings file, then the aggregator writes its own."""
    run_dir = Path(out["run_dir"])
    members = out["panel"]["members"]
    write_findings(run_dir, {m: (per_member or {}).get(m, []) for m in members})
    write_findings(run_dir, {"aggregator": aggregated if aggregated is not None else {"findings": [], "dropped": []}})
    return run_dir


def pr_setup(tmp_path, **config_over):
    origin, clone = make_origin_and_clone(tmp_path)
    sha = push_branch(tmp_path, origin, "feature", {"app.py": "x = 1\ny = 2\n"})
    provider = FakeProvider()
    pr = provider.add_pr(head_ref="feature", head_sha=sha)
    provider.files[pr.ref] = ["app.py"]
    ctx = make_ctx(tmp_path, clone, provider=provider, **config_over)
    return ctx, provider, pr


def run_pr_context(ctx, pr):
    code, out = call(ctx, "precheck", "--mode", "pr", "--pr", str(pr.ref))
    assert out["proceed"], out
    code, out = call(ctx, "context", "--mode", "pr", "--pr", str(pr.ref))
    assert code == 0, out
    return out
