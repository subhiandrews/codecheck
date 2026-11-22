"""Which directories under the working directory are clones worth looking at (names come from their remotes, elsewhere)."""

from pathlib import Path

from codecheck.errors import CodeCheckError
from codecheck.runner import CommandRunner
from codecheck.vcs.git import Git


def candidate_clones(cwd: Path, runner: CommandRunner, skip: Path | None = None) -> list[Path]:
    """The clone `cwd` is inside, else the immediate subdirectories that hold a `.git` directory, by name.

    Dot-directories, symlinks, linked worktrees (their `.git` is a file) and anything under `skip` are left out."""
    try:
        return [Git(runner, cwd).toplevel()]
    except CodeCheckError:
        pass
    try:
        children = sorted(p for p in cwd.iterdir() if not p.name.startswith(".") and not p.is_symlink())
    except OSError:
        return []
    return [p for p in children if (p / ".git").is_dir() and not (skip and p.resolve().is_relative_to(skip.resolve()))]
