from dataclasses import asdict

import pytest

from codecheck.config import load_config
from codecheck.errors import ConfigError

MEMORY_DEFAULTS = {
    "enabled": False, "read": False, "reviewers": False, "store": "~/.local/share/code-check/memory",
    "max_lessons": 12, "max_chars": 6000, "timeout_seconds": 3, "url": "", "token_file": "",
    "share_level": "lessons", "private_level": "lessons", "share_repos": (), "embed_url": "http://127.0.0.1:11434",
    "embed_model": "", "embed_at": "client", "collect": False, "half_life_days": 90, "expire_days": 180,
}


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_memory_defaults(tmp_path):
    assert asdict(load_config(tmp_path / "repo", tmp_path / "home", {}).memory) == MEMORY_DEFAULTS


def test_repo_file_cannot_set_memory_and_user_config_wins(tmp_path):
    write(tmp_path / "repo/.code-check.toml", "[memory]\nenabled = true\n")
    with pytest.raises(ConfigError, match=r"memory.*repository"):
        load_config(tmp_path / "repo", tmp_path / "home", {})
    write(tmp_path / "home/.config/code-check/config.toml", "[memory]\nenabled = true\nmax_lessons = 5\n")
    (tmp_path / "repo/.code-check.toml").unlink()
    cfg = load_config(tmp_path / "repo", tmp_path / "home", {})
    assert (cfg.memory.enabled, cfg.memory.max_lessons) == (True, 5)


def test_a_repo_file_cannot_set_a_single_memory_key_either(tmp_path):
    write(tmp_path / "repo/.code-check.toml", '[memory]\nurl = "https://evil.example.com"\n')
    with pytest.raises(ConfigError, match=r"memory.*repository"):
        load_config(tmp_path / "repo", tmp_path / "home", {})


def test_memory_env_overrides(tmp_path):
    env = {"CODE_CHECK_MEMORY__ENABLED": "1", "CODE_CHECK_MEMORY__MAX_CHARS": "100",
           "CODE_CHECK_MEMORY__SHARE_REPOS": "o/a, o/b", "CODE_CHECK_MEMORY__SHARE_LEVEL": "evidence",
           "CODE_CHECK_MEMORY__URL": "https://mem.example.com", "CODE_CHECK_MEMORY__EMBED_AT": "server"}
    m = load_config(tmp_path / "repo", tmp_path / "home", env).memory
    assert (m.enabled, m.max_chars, m.share_repos, m.share_level, m.url, m.embed_at) == (
        True, 100, ("o/a", "o/b"), "evidence", "https://mem.example.com", "server")


@pytest.mark.parametrize("env", [
    {"CODE_CHECK_MEMORY__SHARE_LEVEL": "everything"},
    {"CODE_CHECK_MEMORY__PRIVATE_LEVEL": "diffs"},
    {"CODE_CHECK_MEMORY__EMBED_AT": "cloud"},
    {"CODE_CHECK_MEMORY__MAX_LESSONS": "0"},
    {"CODE_CHECK_MEMORY__MAX_CHARS": "-1"},
    {"CODE_CHECK_MEMORY__TIMEOUT_SECONDS": "0"},
    {"CODE_CHECK_MEMORY__HALF_LIFE_DAYS": "0"},
    {"CODE_CHECK_MEMORY__EXPIRE_DAYS": "0"},
    {"CODE_CHECK_MEMORY__URL": "http://mem.example.com"},
    {"CODE_CHECK_MEMORY__URL": "ftp://mem.example.com"},
    {"CODE_CHECK_MEMORY__URL": "https://user:pw@mem.example.com"},
    {"CODE_CHECK_MEMORY__URL": "https://"},
    {"CODE_CHECK_MEMORY__EMBED_URL": "http://embed.example.com:11434"},
    {"CODE_CHECK_MEMORY__EMBED_URL": ""},
    {"CODE_CHECK_MEMORY__SHARE_REPOS": "-rf"},
    {"CODE_CHECK_MEMORY__STORE": "--evil"},
])
def test_memory_validation_rejects(tmp_path, env):
    with pytest.raises(ConfigError, match="memory"):
        load_config(tmp_path / "repo", tmp_path / "home", env)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080", "http://localhost:1", "https://mem.example.com", "http://[::1]:8"])
def test_memory_urls_accept_https_or_loopback(tmp_path, url):
    assert load_config(tmp_path / "repo", tmp_path / "home", {"CODE_CHECK_MEMORY__URL": url}).memory.url == url
