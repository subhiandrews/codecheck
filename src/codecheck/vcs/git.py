"""Git operations through a CommandRunner. This layer never force-pushes."""

from collections.abc import Sequence
from pathlib import Path

from codecheck.errors import GitError
from codecheck.runner import CommandResult, CommandRunner


class Git:
    def __init__(self, runner: CommandRunner, cwd: Path | str) -> None:
        self.runner = runner
        self.cwd = Path(cwd)

    def _run(self, *args: str) -> CommandResult:
        return self.runner.run(["git", "-c", "core.quotepath=false", *args], cwd=self.cwd)

    def _raw(self, *args: str) -> str:
        res = self._run(*args)
        if res.returncode != 0:
            raise GitError(f"git {' '.join(args[:3])} failed: {res.stderr.strip()}")
        return res.stdout

    def _out(self, *args: str) -> str:
        return self._raw(*args).strip()

    def fetch(self, remote: str, *refspecs: str) -> bool:
        return self._run("fetch", "--quiet", remote, *refspecs).returncode == 0

    def ref_exists(self, ref: str) -> bool:
        return self._run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode == 0

    def default_branch(self, remote: str = "origin") -> str:
        res = self._run("symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD")
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip().removeprefix(f"{remote}/")
        for candidate in ("main", "master"):
            if self.ref_exists(f"{remote}/{candidate}"):
                return candidate
        raise GitError("cannot determine the default branch; set base_branch in .code-check.toml")

    def rev_parse(self, ref: str) -> str:
        return self._out("rev-parse", ref)

    def merge_base(self, a: str, b: str) -> str:
        return self._out("merge-base", a, b)

    def diff(self, range_expr: str) -> str:
        return self._raw("diff", "--no-color", "--no-ext-diff", range_expr)

    def untracked(self) -> list[str]:
        out = self._raw("ls-files", "--others", "--exclude-standard", "-z")
        return [p for p in out.split("\0") if p]

    def local_diff(self, base_sha: str) -> str:
        parts = [self._raw("diff", "--no-color", "--no-ext-diff", base_sha)]
        for path in self.untracked():
            res = self._run("diff", "--no-color", "--no-index", "--", "/dev/null", path)
            if res.returncode in (0, 1):  # 1 just means "files differ"
                parts.append(res.stdout)
        return "".join(parts)

    def is_dirty(self) -> bool:
        return bool(self._out("status", "--porcelain"))

    def current_branch(self) -> str:
        return self._out("rev-parse", "--abbrev-ref", "HEAD")

    def head_sha(self) -> str:
        return self._out("rev-parse", "HEAD")

    def commit_time(self, ref: str) -> int:
        return int(self._out("log", "-1", "--format=%ct", ref))

    def behind_count(self, base_ref: str) -> int:
        return int(self._out("rev-list", "--count", f"HEAD..{base_ref}"))

    def remote_url(self, remote: str) -> str:
        return self._out("remote", "get-url", remote)

    def toplevel(self) -> Path:
        return Path(self._out("rev-parse", "--show-toplevel"))

    def commit(self, message: str, paths: Sequence[str]) -> str:
        if not paths:
            raise GitError("commit requires explicit paths; refusing to stage everything")
        self._raw("add", "--", *paths)
        self._raw("commit", "-m", message)
        return self.head_sha()

    def push(self, remote: str, branch: str) -> None:
        # Deliberately no force options anywhere in this layer.
        self._raw("push", remote, f"HEAD:refs/heads/{branch}")

    def worktree_add(self, path: Path, ref: str) -> None:
        self._raw("worktree", "add", "--detach", str(path), ref)

    def worktree_remove(self, path: Path) -> None:
        self._run("worktree", "remove", "--force", str(path))
        self._run("worktree", "prune")
