import pytest

from codecheck.evals import history


@pytest.fixture(autouse=True)
def eval_history_dir(tmp_path_factory, monkeypatch):
    """Evals record into the repository's docs/ by default; tests must never touch the real history."""
    monkeypatch.setattr(history, "DEFAULT_DIR", tmp_path_factory.mktemp("eval-history-docs"))


@pytest.fixture
def git_env(monkeypatch):
    """Deterministic git identity/config for tests that run real git."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
