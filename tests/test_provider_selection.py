"""Choosing between GitHub and GitLab must never change what a GitHub setup does."""

import subprocess

import pytest
from clihelp import call, make_ctx
from fakes import FakeProvider, FakeRunner
from gitutil import make_origin_and_clone

from codecheck.cli import build_context
from codecheck.errors import CodeCheckError
from codecheck.models import PRRef
from codecheck.providers import detect_provider
from codecheck.providers.github import GitHubProvider
from codecheck.providers.gitlab import GitLabProvider


@pytest.mark.parametrize("url", [
    "https://github.com/o/r.git", "git@github.com:o/r.git", "ssh://git@github.com/o/r", "/local/path/origin.git",
    "https://ghe.example.com/o/r", "https://gitlab.com.evil.org/o/r", "https://notgitlab.com/o/r",
    "https://example.com/gitlab.com/o/r", "", "not a url",
])
def test_any_remote_but_gitlab_com_keeps_github(url):
    assert detect_provider("auto", url) == "github"


@pytest.mark.parametrize("url", [
    "https://gitlab.com/g/p.git", "git@gitlab.com:g/s/p.git", "https://oauth2:tok@gitlab.com/g/p", "ssh://git@gitlab.com:2222/g/p.git",
    "https://GitLab.com/g/p",
])
def test_a_gitlab_com_remote_selects_gitlab_under_auto(url):
    assert detect_provider("auto", url) == "gitlab"


def test_an_explicit_choice_is_never_overridden():
    assert detect_provider("gitlab", "https://github.com/o/r.git") == "gitlab"
    assert detect_provider("github", "https://gitlab.com/g/p") == "github"
    assert detect_provider("bitbucket", "https://gitlab.com/g/p") == "bitbucket"  # unknown names still fail in get_provider


def test_the_default_provider_is_auto():
    from codecheck.config import Config

    assert Config().provider == "auto"


def _clone_with_remote(tmp_path, url):
    _, clone = make_origin_and_clone(tmp_path)
    subprocess.run(["git", "-C", str(clone), "remote", "set-url", "origin", url], check=True)
    return clone


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/o/r.git", GitHubProvider),
    ("git@github.com:o/r.git", GitHubProvider),
    ("https://gitlab.com/g/s/p.git", GitLabProvider),
])
def test_build_context_picks_the_provider_from_the_remote(tmp_path, url, expected):
    clone = _clone_with_remote(tmp_path, url)
    ctx = build_context(clone, {}, tmp_path / "home")
    assert type(ctx.provider) is expected


def test_an_explicit_provider_in_config_beats_the_remote(tmp_path):
    clone = _clone_with_remote(tmp_path, "https://gitlab.com/g/p.git")
    home = tmp_path / "home"
    (home / ".config" / "code-check").mkdir(parents=True)
    (home / ".config" / "code-check" / "config.toml").write_text('provider = "github"\n')
    assert type(build_context(clone, {}, home).provider) is GitHubProvider


def test_build_context_outside_a_repository_stays_on_github(tmp_path):
    (tmp_path / "plain").mkdir()
    assert type(build_context(tmp_path / "plain", {}, tmp_path / "home").provider) is GitHubProvider


def test_github_declares_flat_repository_paths():
    assert GitHubProvider(FakeRunner()).nested_repos is False and FakeProvider().nested_repos is False
    assert GitLabProvider(FakeRunner()).nested_repos is True


def test_prref_parse_is_strict_by_default_and_nested_only_on_request():
    assert PRRef.parse("o/r#7") == PRRef("o/r", 7)
    assert PRRef.parse("g/s/p#7", nested=True) == PRRef("g/s/p", 7)
    assert PRRef.parse("o/r#7", nested=True) == PRRef("o/r", 7)
    for bad in ("g/s/p#7", "a/b/c/d#1"):
        with pytest.raises(ValueError):
            PRRef.parse(bad)
    for bad in ("p#7", "g/s/p", "g/p#x", ""):
        with pytest.raises(ValueError):
            PRRef.parse(bad, nested=True)


def test_github_references_resolve_exactly_as_before(tmp_path):
    _, clone = make_origin_and_clone(tmp_path)
    ctx = make_ctx(tmp_path, clone, provider=FakeProvider())
    assert ctx.resolve_ref("o/r#7") == PRRef("o/r", 7)
    assert ctx.resolve_ref("https://github.com/o/r/pull/7") == PRRef("o/r", 7)
    assert ctx.resolve_ref("https://github.com/o/r/pull/7/files") == PRRef("o/r", 7)
    assert ctx.resolve_ref("7") == PRRef("o/r", 7)
    for bad in ("g/s/p#7", "https://gitlab.com/g/p/-/merge_requests/7", "x", "o/r#x"):
        with pytest.raises(CodeCheckError):
            ctx.resolve_ref(bad)
    with pytest.raises(CodeCheckError):
        ctx.parse_ref("g/s/p#7")


def test_gitlab_references_accept_merge_request_urls_and_nested_paths(tmp_path):
    ctx = make_ctx(tmp_path, tmp_path, provider=GitLabProvider(FakeRunner()))
    assert ctx.resolve_ref("g/s/p#7") == PRRef("g/s/p", 7)
    assert ctx.resolve_ref("https://gitlab.com/g/s/p/-/merge_requests/7") == PRRef("g/s/p", 7)
    assert ctx.resolve_ref("https://gitlab.com/g/p/-/merge_requests/7/diffs") == PRRef("g/p", 7)
    assert ctx.parse_ref("g/s/p#7") == PRRef("g/s/p", 7)


def test_doctor_checks_gh_for_github_and_glab_for_gitlab(tmp_path):
    def names(provider):
        r = FakeRunner()
        for argv in (["git", "--version"], ["gh", "--version"], ["gh", "auth", "status"], ["glab", "--version"],
                     ["glab", "auth", "status"], ["claude", "--version"]):
            r.on(*argv, stdout="ok\n")
        _, out = call(make_ctx(tmp_path, tmp_path, provider=provider, runner=r), "doctor")
        return [c["name"] for c in out["checks"]], [a[0] for a in r.argvs()]

    assert names(FakeProvider()) == (["python", "git", "gh", "gh auth", "claude"], ["git", "gh", "gh", "claude"])
    assert names(GitHubProvider(FakeRunner())) == (["python", "git", "gh", "gh auth", "claude"], ["git", "gh", "gh", "claude"])
    assert names(GitLabProvider(FakeRunner())) == (["python", "git", "glab", "glab auth", "claude"], ["git", "glab", "glab", "claude"])
