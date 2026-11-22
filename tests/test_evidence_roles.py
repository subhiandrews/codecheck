"""Phase 1a: who said what (roles), `can_write`, and the new fields on shadow records."""

import json

import pytest
from fakes import FakeProvider, FakeRunner

from codecheck.errors import ProviderError
from codecheck.models import PRRef, Thread, ThreadComment
from codecheck.providers.github import THREADS_QUERY, GitHubProvider
from codecheck.providers.gitlab import GitLabProvider
from codecheck.shadow.collect import collect_outcomes
from codecheck.shadow.export import export_golden
from codecheck.shadow.recorder import record_run
from codecheck.shadow.roles import RoleResolver, derive_role
from codecheck.shadow.store import ShadowStore

RID = "abcdef012345"
REF = PRRef("o/r", 7)
FID = "a" * 12
MARK = f"**Warning**\n<!-- code-check:finding:{FID} -->"


@pytest.mark.parametrize("login,can_write,assoc,expected", [
    ("me", True, "OWNER", "self"),
    ("pat", True, "OWNER", "author"),  # the PR author stays `author` even with write access
    ("pat", None, "", "author"),
    ("maya", True, "", "maintainer"),
    ("maya", True, "NONE", "maintainer"),  # can_write beats the association
    ("maya", False, "OWNER", "contributor"),
    ("maya", None, "OWNER", "maintainer"),
    ("maya", None, "MEMBER", "unknown"),  # an org member is not known to have write access
    ("maya", None, "COLLABORATOR", "maintainer"),
    ("maya", None, "CONTRIBUTOR", "contributor"),
    ("maya", None, "FIRST_TIME_CONTRIBUTOR", "contributor"),
    ("maya", None, "NONE", "contributor"),
    ("maya", None, "collaborator", "maintainer"),
    ("maya", None, "", "unknown"),
    ("maya", None, "SOMETHING_NEW", "unknown"),
])
def test_role_derivation_table(login, can_write, assoc, expected):
    assert derive_role(login, me="me", pr_author="pat", can_write=can_write, association=assoc) == expected


def test_role_without_a_known_pr_author_never_calls_anyone_author():
    assert derive_role("pat", me="me", pr_author=None, can_write=True) == "maintainer"


def test_resolver_caches_per_login_and_skips_lookups_for_self_and_author():
    provider = FakeProvider()
    provider.write_access = {"maya": True}
    roles = RoleResolver(provider, "o/r", "me", "pat")
    assert [roles.role(x) for x in ("maya", "maya", "me", "pat", "maya")] == ["maintainer", "maintainer", "self", "author", "maintainer"]
    assert provider.can_write_calls == [("o/r", "maya")]


def test_resolver_swallows_lookup_errors_and_odd_answers():
    provider = FakeProvider()
    provider.write_errors = {"boom"}
    provider.write_access = {"odd": "yes"}  # type: ignore[dict-item]
    roles = RoleResolver(provider, "o/r", "me", "pat")
    assert roles.role("boom", "COLLABORATOR") == "maintainer"  # falls back to the association
    assert roles.role("odd") == "unknown"
    assert roles.role("boom") == "unknown"
    assert provider.can_write_calls.count(("o/r", "boom")) == 1  # a failure is cached too


class NoCanWrite:
    """A provider from before this change."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        if name == "can_write":
            raise AttributeError(name)
        return getattr(self._inner, name)


def seed(shadow, **run):
    shadow.append(RID, {"type": "run", "pr": "o/r#7", "repo": "o/r", "head_sha": "abc", "panel": [], "models": {},
                        "tool_version": "0", **run})
    shadow.append(RID, {"type": "finding", "id": FID, "file": "a.py", "line": 3, "severity": "warning",
                        "category": "bug", "message": "m", "reviewers": ["skeptic"], "inline": True})


def tc(cid, author, body, association=""):
    return ThreadComment(cid, author, body, "t", association=association)


def provider_with(*threads):
    provider = FakeProvider()
    provider.add_pr(7, author="pat")
    provider.threads[REF] = list(threads)
    return provider


def test_collect_records_roles_on_replies_resolution_and_human_comments(tmp_path):
    shadow = ShadowStore(tmp_path)
    seed(shadow, pr_author="pat")
    provider = provider_with(
        Thread("T1", "a.py", 3, True, False,
               (tc(1, "me", MARK), tc(2, "maya", "no"), tc(3, "pat", "disagree"), tc(4, "zed", "hm", "CONTRIBUTOR"),
                tc(5, "kim", "?", "")), "maya"),
        Thread("T2", "b.py", 9, False, False, (tc(6, "maya", "you missed this", "MEMBER"),)),
    )
    provider.write_access = {"maya": True, "zed": False}
    collect_outcomes(provider, shadow, REF, "me")
    recs = shadow.read(RID)
    (outcome,) = (r for r in recs if r["type"] == "outcome")
    assert [(r["author"], r["role"]) for r in outcome["replies"]] == [
        ("maya", "maintainer"), ("pat", "author"), ("zed", "contributor"), ("kim", "unknown")]
    assert outcome["resolved_by_role"] == "maintainer"
    (human,) = (r for r in recs if r["type"] == "human_comment")
    assert human["role"] == "maintainer"
    assert provider.can_write_calls.count(("o/r", "maya")) == 1  # one lookup per login per collect call
    assert provider.thread_calls  # sanity: the fake was used


def test_collect_falls_back_to_association_when_can_write_fails_and_never_raises(tmp_path):
    shadow = ShadowStore(tmp_path)
    seed(shadow, pr_author="pat")
    provider = provider_with(Thread("T1", "a.py", 3, False, False,
                                    (tc(1, "me", MARK), tc(2, "maya", "no", "COLLABORATOR"), tc(3, "zed", "no"))))
    provider.write_errors = {"maya", "zed"}
    assert collect_outcomes(provider, shadow, REF, "me")["outcomes"] == 1
    (outcome,) = (r for r in shadow.read(RID) if r["type"] == "outcome")
    assert [r["role"] for r in outcome["replies"]] == ["maintainer", "unknown"]
    assert outcome["resolved_by_role"] is None


def test_collect_works_with_a_provider_that_has_no_can_write(tmp_path):
    shadow = ShadowStore(tmp_path)
    seed(shadow)
    provider = provider_with(Thread("T1", "a.py", 3, False, False, (tc(1, "me", MARK), tc(2, "maya", "no", "OWNER"))))
    assert collect_outcomes(NoCanWrite(provider), shadow, REF, "me")["outcomes"] == 1
    (outcome,) = (r for r in shadow.read(RID) if r["type"] == "outcome")
    assert outcome["replies"][0]["role"] == "maintainer"


def test_pr_author_comes_from_the_host_when_the_run_record_is_old(tmp_path):
    shadow = ShadowStore(tmp_path)
    seed(shadow)  # no pr_author field: a record written before this change
    provider = provider_with(Thread("T1", "a.py", 3, False, False, (tc(1, "me", MARK), tc(2, "pat", "no"))))
    collect_outcomes(provider, shadow, REF, "me")
    (outcome,) = (r for r in shadow.read(RID) if r["type"] == "outcome")
    assert outcome["replies"][0]["role"] == "author"
    # and when the host cannot say either, the role degrades instead of failing
    shadow2 = ShadowStore(tmp_path / "two")
    seed(shadow2)
    provider2 = provider_with(Thread("T1", "a.py", 3, False, False, (tc(1, "me", MARK), tc(2, "pat", "no", "OWNER"))))
    del provider2.prs[REF]
    collect_outcomes(provider2, shadow2, REF, "me")
    (outcome2,) = (r for r in shadow2.read(RID) if r["type"] == "outcome")
    assert outcome2["replies"][0]["role"] == "maintainer"


def test_old_records_without_the_new_fields_still_load_and_export(tmp_path):
    shadow = ShadowStore(tmp_path)
    seed(shadow)
    shadow.append(RID, {"type": "outcome", "finding_id": FID, "thread_id": "T1", "resolved": True, "outdated": False,
                        "replies": [{"author": "alice", "body": "not a bug"}], "reactions": {}})
    shadow.save_diff(RID, "diff --git a/a.py b/a.py\n")
    recs = shadow.read(RID)
    assert "pr_author" not in recs[0] and "role" not in recs[2]["replies"][0]
    export_golden(shadow, tmp_path / "out")  # consumers must not need the new fields


def test_record_run_records_the_pr_author(tmp_path):
    from codecheck.config import Config

    store = ShadowStore(tmp_path)
    meta = {"run_id": RID, "skill": "review-pr", "repo": "o/r", "pr": "o/r#7", "pr_author": "pat", "head_sha": "abc",
            "mode": "pr", "panel": {"members": []}}
    record_run(store, meta, "", Config())
    assert store.read(RID)[0]["pr_author"] == "pat"
    record_run(store, {**meta, "run_id": "fedcba012345", "pr_author": None}, "", Config())  # local mode
    assert store.read("fedcba012345")[0]["pr_author"] is None


# --- GraphQL mapping and can_write on the real adapters (scripted runner, no network) ---

def _node(**over):
    base = {"id": "T", "isResolved": True, "isOutdated": False, "path": "a.py", "line": 5, "originalLine": 4,
            "resolvedBy": {"login": "maya"},
            "comments": {"nodes": [
                {"databaseId": 1, "author": {"login": "me"}, "authorAssociation": "NONE", "body": "b",
                 "createdAt": "t", "reactions": {"nodes": []}},
                {"databaseId": 2, "author": {"login": "maya"}, "authorAssociation": "MEMBER", "body": "c",
                 "createdAt": "t", "reactions": {"nodes": []}},
                {"databaseId": 3, "author": None, "body": "gone", "createdAt": "t", "reactions": {"nodes": []}},
            ]}}
    return {**base, **over}


def _page(*nodes):
    return json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {
        "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": list(nodes)}}}}})


def test_the_threads_query_asks_for_association_and_resolver():
    assert "authorAssociation" in THREADS_QUERY and "resolvedBy{login}" in THREADS_QUERY


def test_graphql_mapping_of_association_and_resolved_by():
    argv = ["gh", "api", "graphql", "-f", f"query={THREADS_QUERY}", "-f", "owner=o", "-f", "name=r", "-F", "number=7"]
    gh = GitHubProvider(FakeRunner().on(*argv, stdout=_page(_node(), _node(id="U", resolvedBy=None, isResolved=False))))
    first, second = gh.list_threads(REF)
    assert [c.association for c in first.comments] == ["NONE", "MEMBER", ""]  # missing field: ""
    assert first.resolved_by == "maya" and second.resolved_by is None


def perm_argv(repo="o/r", login="maya"):
    return ("gh", "api", f"repos/{repo}/collaborators/{login}/permission", "--jq", ".permission")


@pytest.mark.parametrize("out,expected", [
    ("admin\n", True), ("maintain\n", True), ("write\n", True),
    ("read\n", False), ("triage\n", False), ("none\n", False),
    ("", None), ("surprise\n", None), ("null\n", None),
])
def test_github_can_write_maps_permission(out, expected):
    assert GitHubProvider(FakeRunner().on(*perm_argv(), stdout=out)).can_write("o/r", "maya") is expected


def test_github_can_write_is_none_on_error_and_never_runs_with_invalid_input():
    assert GitHubProvider(FakeRunner().on(*perm_argv(), returncode=1, stderr="HTTP 403")).can_write("o/r", "maya") is None
    runner = FakeRunner()
    gh = GitHubProvider(runner)
    for repo, login in (("-x/y", "maya"), ("o/r", "--paginate"), ("o/r", "a/b"), ("o/r", "-maya"), ("o/r", "")):
        assert gh.can_write(repo, login) is None
    assert runner.calls == []  # nothing from a PR reached argv


def test_github_can_write_accepts_a_bot_login():
    gh = GitHubProvider(FakeRunner().on(*perm_argv(login="dependabot[bot]"), stdout="read\n"))
    assert gh.can_write("o/r", "dependabot[bot]") is False


def _gl(members):
    return (FakeRunner().on("glab", "api", "users?username=maya", stdout='[{"id": 9}]')
            .on("glab", "api", "projects/g%2Fs%2Fp/members/all/9", stdout=members))


@pytest.mark.parametrize("members,expected", [
    ('{"access_level": 50}', True), ('{"access_level": 30}', True), ('{"access_level": 20}', False),
    ('{"access_level": 10}', False), ("{}", None), ("[]", None), ('{"access_level": "30"}', None),
])
def test_gitlab_can_write_uses_access_level_30(members, expected):
    assert GitLabProvider(_gl(members)).can_write("g/s/p", "maya") is expected


def test_gitlab_can_write_is_none_when_unknown_or_failing():
    none_found = FakeRunner().on("glab", "api", "users?username=maya", stdout="[]")
    assert GitLabProvider(none_found).can_write("g/s/p", "maya") is None
    not_member = FakeRunner().on("glab", "api", "users?username=maya", stdout='[{"id": 9}]').on(
        "glab", "api", "projects/g%2Fs%2Fp/members/all/9", returncode=1, stderr="404 Not found")
    assert GitLabProvider(not_member).can_write("g/s/p", "maya") is None
    runner = FakeRunner()
    gl = GitLabProvider(runner)
    for repo, login in (("-x/y", "maya"), ("g/p", "-maya"), ("g/p", "a b")):
        assert gl.can_write(repo, login) is None
    assert runner.calls == []


def test_the_fake_provider_matches_the_protocol():
    provider = FakeProvider()
    provider.write_access = {"a": True}
    assert provider.can_write("o/r", "a") is True and provider.can_write("o/r", "b") is None
    provider.write_errors = {"c"}
    with pytest.raises(ProviderError):
        provider.can_write("o/r", "c")
