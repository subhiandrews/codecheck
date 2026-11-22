import os
import subprocess
from pathlib import Path

# CI runners have no git identity; commits would fail with status 128 for any test
# that does not use the `git_env` fixture. A value already in the environment wins.
_IDENTITY = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def sh(cwd: Path, *args: str) -> str:
    env = {**_IDENTITY, **os.environ}
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout


def make_origin_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """Bare origin on `main` with one commit, plus a clone of it."""
    origin = tmp_path / "origin.git"
    sh(tmp_path, "init", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    sh(tmp_path, "clone", str(origin), str(seed))
    (seed / "README.md").write_text("hello\n")
    sh(seed, "add", "README.md")
    sh(seed, "commit", "-m", "init")
    sh(seed, "push", "origin", "HEAD:main")
    clone = tmp_path / "clone"
    sh(tmp_path, "clone", str(origin), str(clone))
    return origin, clone


def commit_file(repo: Path, name: str, text: str, message: str) -> None:
    (repo / name).write_text(text)
    sh(repo, "add", name)
    sh(repo, "commit", "-m", message)


def push_branch(tmp_path: Path, origin: Path, branch: str, files: dict[str, str]) -> str:
    """Create `branch` on origin with the given files committed; return its head SHA."""
    work = tmp_path / f"work-{branch}"
    sh(tmp_path, "clone", str(origin), str(work))
    sh(work, "checkout", "-b", branch)
    for name, text in files.items():
        (work / name).write_text(text)
        sh(work, "add", name)
    sh(work, "commit", "-m", f"work on {branch}")
    sh(work, "push", "origin", branch)
    return sh(work, "rev-parse", "HEAD").strip()
