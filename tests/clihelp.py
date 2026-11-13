import io
import json
from dataclasses import replace
from pathlib import Path

from codecheck.cli import AppContext, main
from codecheck.config import Config, ShadowConfig
from codecheck.core.state import StateStore
from codecheck.runner import SubprocessRunner
from codecheck.shadow.store import ShadowStore
from codecheck.vcs.git import Git
from fakes import FakeProvider


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
