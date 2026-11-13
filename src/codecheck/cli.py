"""Command-line entry point. Every subcommand prints one JSON document on stdout."""

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from codecheck import __version__, commands
from codecheck.config import Config, expand_path, load_config
from codecheck.core.state import StateStore
from codecheck.errors import CodeCheckError
from codecheck.models import PRRef
from codecheck.providers import get_provider
from codecheck.providers.base import Provider
from codecheck.runids import check_run_id
from codecheck.runner import CommandRunner, SubprocessRunner
from codecheck.shadow.store import ShadowStore
from codecheck.vcs.git import Git

_PR_URL = re.compile(r"([^/\s]+/[^/\s]+)/pull/(\d+)")


@dataclass
class AppContext:
    config: Config
    runner: CommandRunner
    provider: Provider
    git: Git
    state: StateStore
    shadow: ShadowStore | None
    cwd: Path
    env: Mapping[str, str]

    def run_dir(self, run_id: str) -> Path:
        return expand_path(self.config.state_dir) / "runs" / check_run_id(run_id)

    def record(self, run_id: str, record: dict) -> None:
        if self.shadow is not None:
            self.guarded(self.shadow.append, run_id, record)

    def guarded(self, fn, *args, **kwargs) -> None:
        """Shadow recording must never fail or alter a command: swallow and warn."""
        try:
            fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - recording is best effort by design
            print(f"warning: shadow recording failed: {type(e).__name__}: {e}", file=sys.stderr)

    def current_repo(self) -> str:
        repo = self.provider.repo_from_remote(self.git.remote_url(self.config.remote))
        if not repo:
            raise CodeCheckError(f"cannot work out the repository from remote {self.config.remote!r}")
        return repo

    def resolve_ref(self, text: str) -> PRRef:
        text = text.strip()
        if text.isdigit():
            return PRRef(self.current_repo(), int(text))
        if m := _PR_URL.search(text):
            return PRRef(m.group(1), int(m.group(2)))
        try:
            return PRRef.parse(text)
        except ValueError as e:
            raise CodeCheckError(str(e)) from None


def build_context(
    cwd: Path, env: Mapping[str, str], home: Path, runner: CommandRunner | None = None, provider: Provider | None = None
) -> AppContext:
    runner = runner or SubprocessRunner()
    git = Git(runner, cwd)
    try:
        cwd = git.toplevel()
        git = Git(runner, cwd)
    except CodeCheckError:
        pass  # not inside a repository; commands that need one will say so
    config = load_config(cwd, home, env)
    provider = provider or get_provider(config.provider, runner, cwd)
    shadow = ShadowStore(expand_path(config.shadow.store)) if config.shadow.enabled or config.shadow.handoff else None
    state = StateStore(expand_path(config.state_dir) / "state.json")
    return AppContext(config, runner, provider, git, state, shadow, cwd, env)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="code-check", description="Deterministic PR review tooling")
    parser.add_argument("--version", action="version", version=f"code-check {__version__}")
    parser.add_argument("--cwd", type=Path, default=None, help="run as if started in this directory")
    sub = parser.add_subparsers(dest="command", required=True)
    for module in commands.ALL:
        p = sub.add_parser(module.NAME, help=module.HELP)
        module.add_arguments(p)
        p.set_defaults(func=module.run)
    return parser


def main(argv: Sequence[str] | None = None, *, ctx: AppContext | None = None, out=None) -> int:
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    try:
        ctx = ctx or build_context((args.cwd or Path.cwd()).resolve(), os.environ, Path.home())
        result = args.func(args, ctx)
    except CodeCheckError as e:
        print(json.dumps({"error": str(e)}), file=out)
        return 1
    except Exception as e:  # noqa: BLE001 - keep the one-JSON-document contract
        import traceback

        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"error": f"unexpected {type(e).__name__}: {e}"}), file=out)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False), file=out)
    return 0


def main_entry() -> None:
    sys.exit(main())
