"""Which directories under the working directory are clones worth looking at (names come from their remotes, elsewhere)."""

from pathlib import Path

from codecheck.errors import CodeCheckError
from codecheck.runner import CommandRunner
from codecheck.vcs.git import Git


def candidate_clones(cwd: Path, runner: CommandRunner, skip: Path | None = None) -> list[Path]:
    """The clones directly under `cwd`, by name; or, when there are none, the clone `cwd` is inside.

    Looking under `cwd` first means a repository that merely contains the directory (a dotfiles repo at `~`) does not
    hide the clones in `~/opensource`. Dot-directories, symlinks, linked worktrees (their `.git` is a file) and
    anything under `skip` are left out."""
    try:
        children = sorted(p for p in cwd.iterdir() if not p.name.startswith(".") and not p.is_symlink())
    except OSError:
        children = []
    under = [p for p in children if (p / ".git").is_dir() and not (skip and p.resolve().is_relative_to(skip.resolve()))]
    if under and not (cwd / ".git").exists():
        return under
    try:
        return [Git(runner, cwd).toplevel()]
    except CodeCheckError:
        return under
