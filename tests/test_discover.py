import os

import pytest
from clihelp import call, make_ctx
from fakes import FakeProvider
from gitutil import sh

from codecheck.cli import _route
from codecheck.errors import ConfigError
from codecheck.models import PRRef
from codecheck.runner import SubprocessRunner
from codecheck.vcs.discover import candidate_clones

pytestmark = pytest.mark.usefixtures("git_env")


class ByUrl(FakeProvider):
    """Names a repository from the last two parts of its remote URL, lists open PRs per repository."""

    def repo_from_remote(self, url):
        owner_repo = url.removesuffix(".git").rsplit("/", 2)[-2:]
        return "/".join(owner_repo) if len(owner_repo) == 2 and url.startswith("https://") else None

    def list_prs(self, scope, repos):
        return [r for r in self.listing[scope] if r.repo in repos]


def _clone(parent, name, remote="https://example.com/o/{}.git"):
    path = parent / name
    path.mkdir()
    sh(path, "init", "-q", "-b", "main")
    if remote:
        sh(path, "remote", "add", "origin", remote.format(name))
    return path


@pytest.fixture
def work(tmp_path):
    parent = tmp_path / "work"
    parent.mkdir()
    a, b = _clone(parent, "a"), _clone(parent, "b")
    _clone(parent, "noremote", remote="")
    _clone(parent, "elsewhere", remote="ssh://host/unparseable")
    (parent / "plain").mkdir()
    (parent / ".hidden").mkdir()
    sh(a, "commit", "-q", "--allow-empty", "-m", "x")
    sh(a, "worktree", "add", "-q", str(parent / "linked"), "-b", "w")  # its .git is a file
    os.symlink(b, parent / "link")
    return parent, a, b


def test_inside_a_clone_that_clone_alone_is_found(work):
    _parent, a, _b = work
    assert candidate_clones(a, SubprocessRunner()) == [a.resolve()]


def test_from_a_parent_directory_only_real_clones_directly_under_it_are_candidates(work):
    parent, _a, _b = work
    names = [p.name for p in candidate_clones(parent, SubprocessRunner())]
    assert names == ["a", "b", "elsewhere", "noremote"]  # no plain dir, dot dir, linked worktree or symlink


def test_clones_are_named_by_their_remotes_and_unnameable_ones_skipped(work):
    parent, _a, b = work
    ctx = make_ctx(parent.parent, parent, provider=ByUrl())
    found = ctx.clones()
    assert sorted(found) == ["o/a", "o/b"] and found["o/b"].cwd == b


def test_a_pr_named_by_repository_runs_in_that_repositories_clone(work):
    parent, _a, b = work
    ctx = make_ctx(parent.parent, parent, provider=ByUrl())
    assert _route(ctx, "o/b#3").cwd == b
    assert _route(ctx, "3") is ctx  # a bare number cannot name a repository
    assert _route(ctx, "o/missing#3") is ctx  # no clone: the command reports it as before
    assert _route(ctx, None) is ctx


def test_list_work_spans_the_clones_under_the_directory_with_one_global_cap(work):
    parent, a, b = work
    provider = ByUrl()
    for repo in ("o/a", "o/b"):
        provider.repo = repo
        provider.listing["open"] += [provider.add_pr(n, head_sha=f"{repo}{n}").ref for n in (9, 8)]
    ctx = make_ctx(parent.parent, parent, provider=provider)
    _code, out = call(ctx, "list-work", "--mode", "review")
    assert [(i["pr"], i["clone"]) for i in out["items"]] == [("o/a#9", str(a)), ("o/a#8", str(a)), ("o/b#9", str(b))]
    assert out["considered"] == 4


def test_list_work_outside_any_clone_says_what_to_do(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    ctx = make_ctx(tmp_path, empty, provider=ByUrl())
    code, out = call(ctx, "list-work", "--mode", "babysit")
    assert code == 1 and "directory that holds clones" in out["error"]


def test_a_named_repository_without_a_local_clone_is_still_listed(work):
    parent, _a, _b = work
    provider = ByUrl()
    provider.repo = "x/y"
    provider.me = "me"
    ref = provider.add_pr(1, head_sha="s").ref
    provider.listing["open"] = [ref]
    ctx = make_ctx(parent.parent, parent, provider=provider)
    _code, out = call(ctx, "list-work", "--mode", "review", "--repos", "x/y")
    assert [(i["pr"], i["clone"]) for i in out["items"]] == [(str(ref), "")]
    assert PRRef("x/y", 1) == ref


def test_one_unreachable_clone_is_skipped_and_the_rest_still_listed(work):
    parent, _a, _b = work
    provider = ByUrl()
    provider.repo = "o/b"
    provider.listing["open"] = [provider.add_pr(5, head_sha="s").ref]
    real = provider.list_prs
    provider.list_prs = lambda scope, repos: (_ for _ in ()).throw(RuntimeError("HTTP 502")) if repos == ["o/a"] else real(scope, repos)
    ctx = make_ctx(parent.parent, parent, provider=provider)
    _code, out = call(ctx, "list-work", "--mode", "review")
    assert [i["pr"] for i in out["items"]] == ["o/b#5"]
    assert out["skipped"] == [{"pr": "o/a", "reason": "error: RuntimeError: HTTP 502"}]


def test_clones_after_the_cap_is_met_are_not_asked_about(work):
    parent, _a, _b = work
    provider = ByUrl()
    provider.repo = "o/a"
    provider.listing["open"] = [provider.add_pr(n, head_sha=f"s{n}").ref for n in (3, 2, 1)]
    asked = []
    real = provider.list_prs
    provider.list_prs = lambda scope, repos: asked.append(repos[0]) or real(scope, repos)
    ctx = make_ctx(parent.parent, parent, provider=provider)
    call(ctx, "list-work", "--mode", "review")
    assert asked == ["o/a"]  # three new PRs in the first clone: o/b is never listed


def test_a_clone_with_a_broken_config_is_reported_but_one_without_a_remote_is_not(work, monkeypatch):
    parent, _a, b = work
    ctx = make_ctx(parent.parent, parent, provider=ByUrl())
    original = type(ctx).at

    def at(self, path):
        if path == b:
            raise ConfigError("unknown config key: nope")
        return original(self, path)

    monkeypatch.setattr(type(ctx), "at", at)
    errors: dict[str, str] = {}
    assert sorted(ctx.clones(errors=errors)) == ["o/a"]
    assert errors == {str(b): "unknown config key: nope"}  # `noremote` and `elsewhere` stay out of it
    provider = ctx.provider
    provider.repo = "o/a"
    provider.listing["open"] = [provider.add_pr(1, head_sha="s").ref]
    _code, out = call(ctx, "list-work", "--mode", "review")
    assert [i["pr"] for i in out["items"]] == ["o/a#1"]
    assert out["skipped"] == [{"pr": str(b), "reason": "not a usable clone: unknown config key: nope"}]
