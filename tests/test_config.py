import pytest

from codecheck.config import load_config, model_for
from codecheck.errors import ConfigError


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_defaults(tmp_path):
    cfg = load_config(tmp_path / "repo", tmp_path / "home", {})
    assert cfg.provider == "github"
    assert cfg.review.auto_approve is False
    assert cfg.loop.max_prs_per_iteration == 5
    assert cfg.models.language_experts == "haiku"
    assert cfg.models.aggregator == "opus"
    assert cfg.deep_reviewer.enabled is False


def test_repo_overrides_user_and_env_overrides_repo(tmp_path):
    home, repo = tmp_path / "home", tmp_path / "repo"
    write(home / ".config/code-check/config.toml", "[review]\nmax_files = 10\nauto_approve = true\n")
    write(repo / ".code-check.toml", "[review]\nmax_files = 20\n")
    cfg = load_config(repo, home, {})
    assert (cfg.review.max_files, cfg.review.auto_approve) == (20, True)
    cfg = load_config(repo, home, {"CODE_CHECK_REVIEW__MAX_FILES": "30", "CODE_CHECK_REVIEW__AUTO_APPROVE": "no"})
    assert (cfg.review.max_files, cfg.review.auto_approve) == (30, False)


def test_env_coerces_lists_and_ignores_unrelated_vars(tmp_path):
    env = {"CODE_CHECK_LOOP__REPOS": "a/b, c/d", "CODE_CHECK_HOME": "/x", "CODE_CHECK_PYTHON": "/y"}
    cfg = load_config(tmp_path, tmp_path, env)
    assert cfg.loop.repos == ("a/b", "c/d")


def test_unknown_key_in_file_is_an_error(tmp_path):
    write(tmp_path / ".code-check.toml", "[review]\nauto_aprove = true\n")
    with pytest.raises(ConfigError, match="auto_aprove"):
        load_config(tmp_path, tmp_path / "h", {})


def test_wrong_type_in_file_is_an_error(tmp_path):
    write(tmp_path / ".code-check.toml", '[review]\nmax_files = "many"\n')
    with pytest.raises(ConfigError, match="max_files"):
        load_config(tmp_path, tmp_path / "h", {})


def test_invalid_toml_is_a_config_error(tmp_path):
    write(tmp_path / ".code-check.toml", "[review\n")
    with pytest.raises(ConfigError):
        load_config(tmp_path, tmp_path / "h", {})


def test_model_for_per_language_override(tmp_path):
    write(tmp_path / ".code-check.toml", '[models.per_language]\nrust = "sonnet"\n')
    cfg = load_config(tmp_path, tmp_path / "h", {})
    assert model_for(cfg.models, "language_expert", "rust") == "sonnet"
    assert model_for(cfg.models, "language_expert", "go") == "haiku"
    assert model_for(cfg.models, "aggregator") == "opus"
    assert model_for(cfg.models, "reviewers") == "sonnet"
