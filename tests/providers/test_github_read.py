import json
import re

import pytest
from fakes import FakeRunner

from codecheck.errors import ProviderError
from codecheck.models import Check, PRRef, Review
from codecheck.providers.github import (
    _REVIEWS_JQ,
    THREADS_QUERY,
    GitHubProvider,
    parse_remote_url,
)

REF = PRRef("o/r", 7)

PR_JSON = json.dumps({
    "number": 7, "title": "Add x", "body": "desc", "author": {"login": "alice"},
    "baseRefName": "main", "headRefName": "feat", "headRefOid": "abc123",
    "isDraft": False, "state": "OPEN", "url": "https://github.com/o/r/pull/7", "isCrossRepository": True,
})


def node(tid, *, resolved=False, outdated=False, line=5, comments=None):
    comments = comments or [("alice", "please fix", 101)]
    return {
        "id": tid, "isResolved": resolved, "isOutdated": outdated, "path": "a.py", "line": line, "originalLine": 4,
        "comments": {"nodes": [
            {"databaseId": cid, "author": ({"login": a} if a else None), "body": b,
             "createdAt": "2026-01-01T00:00:00Z", "reactions": {"nodes": [{"content": "THUMBS_UP"}]}}
            for a, b, cid in comments
        ]},
    }


def page(nodes, *, more=False, cursor=None):
    return json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {
        "pageInfo": {"hasNextPage": more, "endCursor": cursor}, "nodes": nodes}}}}})


def threads_argv(after=None):
    argv = ["gh", "api", "graphql", "-f", f"query={THREADS_QUERY}", "-f", "owner=o", "-f", "name=r", "-F", "number=7"]
    return argv + (["-f", f"after={after}"] if after else [])


def test_whoami_and_get_pr():
    runner = FakeRunner().on("gh", "api", "user", "--jq", ".login", stdout="me\n").on(
        "gh", "pr", "view", "7", "--repo", "o/r", "--json",
        "number,title,body,author,baseRefName,headRefName,headRefOid,isDraft,state,url,isCrossRepository",
        stdout=PR_JSON,
    )
    gh = GitHubProvider(runner)
    assert gh.whoami() == "me"
    pr = gh.get_pr(REF)
    assert (pr.author, pr.base_ref, pr.head_ref, pr.head_sha, pr.state) == ("alice", "main", "feat", "abc123", "open")
    assert pr.is_cross_repo is True and pr.ref == REF


def test_gh_failure_becomes_provider_error():
    runner = FakeRunner().on_prefix("gh", returncode=1, stderr="HTTP 404: Not Found")
    with pytest.raises(ProviderError, match="404"):
        GitHubProvider(runner).get_pr(REF)


def test_list_prs_builds_search_with_repos():
    runner = FakeRunner().on(
        "gh", "search", "prs", "--state", "open", "--review-requested=@me", "--limit", "50",
        "--json", "number,repository", "--repo", "o/r", "--repo", "o/s",
        stdout=json.dumps([{"number": 7, "repository": {"nameWithOwner": "o/r"}}, {"number": 2, "repository": {"nameWithOwner": "o/s"}}]),
    ).on(
        "gh", "search", "prs", "--state", "open", "--author=@me", "--limit", "50", "--json", "number,repository",
        stdout="[]",
    )
    gh = GitHubProvider(runner)
    assert gh.list_prs("review-requested", ["o/r", "o/s"]) == [PRRef("o/r", 7), PRRef("o/s", 2)]
    assert gh.list_prs("authored", []) == []
    with pytest.raises(ProviderError):
        gh.list_prs("bogus", [])


def test_repo_is_private_reads_gh_output_and_returns_none_on_failure():
    argv = ("gh", "repo", "view", "o/r", "--json", "isPrivate", "--jq", ".isPrivate")
    assert GitHubProvider(FakeRunner().on(*argv, stdout="true\n")).repo_is_private("o/r") is True
    assert GitHubProvider(FakeRunner().on(*argv, stdout="false\n")).repo_is_private("o/r") is False
    assert GitHubProvider(FakeRunner().on(*argv, returncode=1, stderr="boom")).repo_is_private("o/r") is None
    assert GitHubProvider(FakeRunner().on(*argv, stdout="weird")).repo_is_private("o/r") is None
    assert GitHubProvider(FakeRunner()).repo_is_private("--bad/x") is None


def test_clone_argv():
    assert GitHubProvider(FakeRunner()).clone_argv("o/r", "/d") == ["gh", "repo", "clone", "o/r", "/d"]


def test_changed_files_and_refspec():
    runner = FakeRunner().on("gh", "pr", "diff", "7", "--repo", "o/r", "--name-only", stdout="a.py\nb/c d.py\n")
    gh = GitHubProvider(runner)
    assert gh.list_changed_files(REF) == ["a.py", "b/c d.py"]
    assert gh.fetch_refspec(REF) == ("+pull/7/head:refs/code-check/pr/7", "refs/code-check/pr/7")


@pytest.mark.parametrize("url,expected", [
    ("git@github.com:o/r.git", "o/r"),
    ("https://github.com/o/r.git", "o/r"),
    ("https://github.com/o/r", "o/r"),
    ("ssh://git@ghe.example.com:22/o/r.git", "o/r"),
    ("not a url", None),
])
def test_parse_remote_url(url, expected):
    assert parse_remote_url(url) == expected


def test_threads_paginate_past_100_and_map_fields():
    runner = FakeRunner().on(*threads_argv(), stdout=page([node("T1")], more=True, cursor="C1")).on(
        *threads_argv("C1"), stdout=page([node("T2", outdated=True, line=None, comments=[(None, "ghost says hi", 202)])]),
    )
    threads = GitHubProvider(runner).list_threads(REF)
    assert [t.id for t in threads] == ["T1", "T2"]
    t1, t2 = threads
    assert (t1.path, t1.line, t1.is_resolved, t1.comments[0].id, t1.comments[0].reactions) == (
        "a.py", 5, False, 101, ("THUMBS_UP",))
    assert t2.is_outdated and t2.line == 4 and t2.comments[0].author == "ghost"
    assert len(runner.calls) == 2


CHECKS_ARGV = ("gh", "pr", "checks", "7", "--repo", "o/r", "--json", "name,bucket,link")
CHECKS_JSON = json.dumps([
    {"name": "build", "bucket": "fail", "link": "https://github.com/o/r/actions/runs/999/job/1"},
    {"name": "lint", "bucket": "pass", "link": ""},
    {"name": "deploy", "bucket": "skipping", "link": "https://example.com"},
    {"name": "e2e", "bucket": "cancel", "link": ""},
    {"name": "slow", "bucket": "pending", "link": ""},
])


def test_checks_accept_nonzero_exit_when_json_present():
    gh = GitHubProvider(FakeRunner().on(*CHECKS_ARGV, stdout=CHECKS_JSON, returncode=1))
    checks = gh.get_checks(REF)
    assert [(c.name, c.state) for c in checks] == [
        ("build", "fail"), ("lint", "pass"), ("deploy", "skipped"), ("e2e", "cancelled"), ("slow", "pending")]
    assert checks[0].run_id == "999" and checks[1].run_id is None


def test_checks_pending_exit_8_and_no_checks_reported():
    assert len(GitHubProvider(FakeRunner().on(*CHECKS_ARGV, stdout=CHECKS_JSON, returncode=8)).get_checks(REF)) == 5
    empty = FakeRunner().on(*CHECKS_ARGV, returncode=1, stderr="no checks reported on the 'feat' branch")
    assert GitHubProvider(empty).get_checks(REF) == []


def test_checks_real_failure_still_raises():
    runner = FakeRunner().on(*CHECKS_ARGV, returncode=1, stderr="authentication required")
    with pytest.raises(ProviderError, match="authentication"):
        GitHubProvider(runner).get_checks(REF)


def test_check_log_is_tail_truncated_and_needs_run_id():
    big = "line\n" * 10_000
    runner = FakeRunner().on("gh", "run", "view", "999", "--repo", "o/r", "--log-failed", stdout=big)
    gh = GitHubProvider(runner)
    log = gh.get_check_log(REF, Check("build", "fail", "", "999"))
    assert 0 < len(log) <= 20_000 and log.endswith("line\n")
    assert gh.get_check_log(REF, Check("lint", "fail", "", None)) == ""


@pytest.mark.parametrize("bad", ["-x/r", "o/-r", "--repo=evil/r", "o/r --flag", "norepo", ""])
def test_repo_values_cannot_act_as_options(bad):
    gh = GitHubProvider(FakeRunner())
    with pytest.raises(ProviderError):
        gh.list_prs("authored", [bad])
    with pytest.raises(ProviderError):
        gh.get_pr(PRRef(bad, 7))
    with pytest.raises(ProviderError):
        gh.list_threads(PRRef(bad, 7))
    with pytest.raises(ProviderError):
        gh.clone_argv(bad, "/d")


def test_clone_dest_leading_dash_rejected_and_numbers_are_ints():
    gh = GitHubProvider(FakeRunner())
    with pytest.raises(ProviderError):
        gh.clone_argv("o/r", "-dest")
    with pytest.raises(ProviderError):
        gh.get_pr(PRRef("o/r", "--web"))  # type: ignore[arg-type]
    with pytest.raises(ProviderError):
        gh.get_check_log(REF, Check("b", "fail", "", "-1"))


def test_threads_query_stays_under_github_node_limit():
    # threads x comments x reactions, summed over every level of the nesting
    threads, comments, reactions = (int(n) for n in re.findall(r"first:(\d+)", THREADS_QUERY))
    assert threads + threads * comments + threads * comments * reactions < 500_000


def test_list_reviews_parses_one_json_object_per_line():
    lines = "\n".join([
        json.dumps({"author": "me", "commit_id": "abc", "submitted_at": "2026-01-01T00:00:00Z", "state": "COMMENTED"}),
        json.dumps({"author": "ghost", "commit_id": "def", "submitted_at": "2026-01-02T00:00:00Z", "state": "APPROVED"}),
        json.dumps({"author": "me", "commit_id": "abc", "submitted_at": None, "state": "PENDING"}),
        "",
    ])
    runner = FakeRunner().on("gh", "api", "repos/o/r/pulls/7/reviews", "--paginate", "--jq", _REVIEWS_JQ, stdout=lines)
    assert GitHubProvider(runner).list_reviews(REF) == [
        Review("me", "abc", "2026-01-01T00:00:00Z", "COMMENTED"),
        Review("ghost", "def", "2026-01-02T00:00:00Z", "APPROVED"),
    ]


def test_list_reviews_tolerates_null_commit_id():
    line = json.dumps({"author": "me", "commit_id": None, "submitted_at": "2026-01-01T00:00:00Z", "state": "COMMENTED"})
    runner = FakeRunner().on("gh", "api", "repos/o/r/pulls/7/reviews", "--paginate", "--jq", _REVIEWS_JQ, stdout=line)
    assert GitHubProvider(runner).list_reviews(REF) == [Review("me", "", "2026-01-01T00:00:00Z", "COMMENTED")]


def test_thread_comment_marks_bots():
    n = node("T1", comments=[("dependabot", "x", 1), ("alice", "y", 2)])
    n["comments"]["nodes"][0]["author"]["__typename"] = "Bot"
    n["comments"]["nodes"][1]["author"]["__typename"] = "User"
    runner = FakeRunner().on(*threads_argv(), stdout=page([n]))
    comments = GitHubProvider(runner).list_threads(REF)[0].comments
    assert [c.is_bot for c in comments] == [True, False]


def test_threads_query_asks_for_author_typename():
    assert "__typename" in THREADS_QUERY


def test_slim_threads_query_has_no_reactions_and_stays_under_node_limit():
    from codecheck.providers.github import THREADS_QUERY_SLIM

    assert "reactions" not in THREADS_QUERY_SLIM
    threads, comments = (int(n) for n in re.findall(r"first:(\d+)", THREADS_QUERY_SLIM))
    assert threads + threads * comments < 500_000


def test_list_threads_without_reactions_uses_slim_query():
    from codecheck.providers.github import THREADS_QUERY_SLIM

    argv = ["gh", "api", "graphql", "-f", f"query={THREADS_QUERY_SLIM}", "-f", "owner=o", "-f", "name=r", "-F", "number=7"]
    runner = FakeRunner().on(*argv, stdout=page([node("T1")]))
    assert [t.id for t in GitHubProvider(runner).list_threads(REF, reactions=False)] == ["T1"]
