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


def test_user_overrides_repo_and_env_overrides_both(tmp_path):
    # Spec section 9: merged in order repo .code-check.toml, then the user file, then the environment.
    home, repo = tmp_path / "home", tmp_path / "repo"
    write(home / ".config/code-check/config.toml", "[review]\nmax_files = 10\nauto_approve = true\n")
    write(repo / ".code-check.toml", "[review]\nmax_files = 20\nmax_inline_comments = 7\n")
    cfg = load_config(repo, home, {})
    assert (cfg.review.max_files, cfg.review.max_inline_comments, cfg.review.auto_approve) == (10, 7, True)
    cfg = load_config(repo, home, {"CODE_CHECK_REVIEW__MAX_FILES": "30", "CODE_CHECK_REVIEW__AUTO_APPROVE": "no"})
    assert (cfg.review.max_files, cfg.review.auto_approve) == (30, False)


@pytest.mark.parametrize("text,key", [
    ("[review]\nauto_approve = true\n", "review.auto_approve"),
    ('state_dir = "/tmp/elsewhere"\n', "state_dir"),
    ('[run_pr]\nbabysit_allowed_tools = ["Bash"]\n', "run_pr"),
    ('[run_pr]\nclaude_bin = "/tmp/evil"\n', "run_pr"),
    ("[shadow]\nenabled = false\n", "shadow"),
    ("[loop]\nreview_forks = true\n", "loop.review_forks"),
])
def test_repo_file_cannot_set_security_sensitive_keys(tmp_path, text, key):
    write(tmp_path / "repo/.code-check.toml", text)
    with pytest.raises(ConfigError, match=rf"{key}.*repository"):
        load_config(tmp_path / "repo", tmp_path / "home", {})


def test_user_file_and_env_may_set_security_sensitive_keys(tmp_path):
    home = tmp_path / "home"
    write(home / ".config/code-check/config.toml", '[run_pr]\nreview_allowed_tools = ["Read", "Write"]\n')
    cfg = load_config(tmp_path / "repo", home, {"CODE_CHECK_REVIEW__AUTO_APPROVE": "true"})
    assert cfg.run_pr.review_allowed_tools == ("Read", "Write") and cfg.review.auto_approve is True


@pytest.mark.parametrize("name", ['"opus\\nname: evil"', '"son net"', '"x/y"', '""'])
def test_model_names_are_validated(tmp_path, name):
    write(tmp_path / ".code-check.toml", f"[models]\naggregator = {name}\n")
    with pytest.raises(ConfigError, match="models.aggregator"):
        load_config(tmp_path, tmp_path / "h", {})


def test_per_language_model_names_are_validated_and_normal_names_pass(tmp_path):
    write(tmp_path / ".code-check.toml", '[models]\nreviewers = "claude-sonnet-5-5[1m]"\n[models.per_language]\nrust = "a\\nb"\n')
    with pytest.raises(ConfigError, match="per_language.rust"):
        load_config(tmp_path, tmp_path / "h", {})
    write(tmp_path / ".code-check.toml", '[models]\nreviewers = "claude-sonnet-5-5[1m]"\n')
    assert load_config(tmp_path, tmp_path / "h", {}).models.reviewers == "claude-sonnet-5-5[1m]"


@pytest.mark.parametrize("env,match", [
    ({"CODE_CHECK_RUN_PR__BABYSIT_ALLOWED_TOOLS": "Read,--dangerously-skip-permissions"}, "babysit_allowed_tools"),
    ({"CODE_CHECK_RUN_PR__REVIEW_ALLOWED_TOOLS": "Read, -x"}, "review_allowed_tools"),
    ({"CODE_CHECK_RUN_PR__PERMISSION_MODE": "--yolo"}, "permission_mode"),
    ({"CODE_CHECK_RUN_PR__REVIEW_PROMPT": "--help {ref}"}, "review_prompt"),
])
def test_run_pr_values_that_could_act_as_options_are_rejected(tmp_path, env, match):
    with pytest.raises(ConfigError, match=match):
        load_config(tmp_path, tmp_path / "h", env)


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


def test_security_defaults(tmp_path):
    cfg = load_config(tmp_path / "repo", tmp_path / "home", {})
    assert cfg.models.security == "opus"
    assert cfg.review.security is False


def test_security_settable_from_repo_file(tmp_path):
    repo = tmp_path / "repo"
    write(repo / ".code-check.toml", '[models]\nsecurity = "sonnet"\n[review]\nsecurity = true\n')
    cfg = load_config(repo, tmp_path / "home", {})
    assert cfg.models.security == "sonnet"
    assert cfg.review.security is True


def test_security_model_name_is_validated(tmp_path):
    home = tmp_path / "home"
    write(home / ".config/code-check/config.toml", '[models]\nsecurity = "bad name!"\n')
    with pytest.raises(ConfigError, match="models.security"):
        load_config(tmp_path / "repo", home, {})


def test_model_for_security():
    from codecheck.config import ModelsConfig
    assert model_for(ModelsConfig(security="sonnet"), "security") == "sonnet"
