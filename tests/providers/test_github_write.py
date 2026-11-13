import json

import pytest
from fakes import FakeRunner

from codecheck.errors import ProviderError
from codecheck.models import Decision, PRRef, ReviewComment, Thread, ThreadComment
from codecheck.providers.github import RESOLVE_MUTATION, GitHubProvider

REF = PRRef("o/r", 7)
REVIEWS = ("gh", "api", "repos/o/r/pulls/7/reviews", "--method", "POST", "--input", "-")
THREAD = Thread("T1", "a.py", 3, False, False, (ThreadComment(101, "alice", "fix", "t"), ThreadComment(102, "me", "ok", "t")))


def test_post_review_sends_one_atomic_payload():
    runner = FakeRunner().on(*REVIEWS, stdout="{}")
    comments = [ReviewComment("a.py", 3, "RIGHT", "**Warning** bug"), ReviewComment("b.py", 9, "RIGHT", "nit")]
    result = GitHubProvider(runner).post_review(REF, "abc123", "summary", comments, Decision("COMMENT", "r"))
    assert (result.posted, result.degraded) == (2, False)
    payload = json.loads(runner.calls[0].input)
    assert payload == {
        "commit_id": "abc123", "body": "summary", "event": "COMMENT",
        "comments": [
            {"path": "a.py", "line": 3, "side": "RIGHT", "body": "**Warning** bug"},
            {"path": "b.py", "line": 9, "side": "RIGHT", "body": "nit"},
        ],
    }


def test_rejected_anchor_degrades_to_body_only_review():
    runner = FakeRunner().on(*REVIEWS, returncode=1, stderr="HTTP 422: Line could not be resolved").on(*REVIEWS, stdout="{}")
    comments = [ReviewComment("a.py", 3, "RIGHT", "**Warning** bug")]
    result = GitHubProvider(runner).post_review(REF, "abc123", "summary", comments, Decision("APPROVE", "r"))
    assert (result.posted, result.degraded) == (0, True)
    retry = json.loads(runner.calls[1].input)
    assert retry["comments"] == [] and retry["event"] == "APPROVE"
    assert "a.py:3" in retry["body"] and "**Warning** bug" in retry["body"] and retry["body"].startswith("summary")


def test_failure_without_inline_comments_raises():
    runner = FakeRunner().on(*REVIEWS, returncode=1, stderr="HTTP 403: forbidden")
    with pytest.raises(ProviderError, match="403"):
        GitHubProvider(runner).post_review(REF, "abc123", "summary", [], Decision("COMMENT", "r"))


def test_degraded_retry_failure_raises():
    runner = FakeRunner().on(*REVIEWS, returncode=1, stderr="422").on(*REVIEWS, returncode=1, stderr="HTTP 500")
    with pytest.raises(ProviderError, match="500"):
        GitHubProvider(runner).post_review(REF, "abc", "s", [ReviewComment("a.py", 1, "RIGHT", "x")], Decision("COMMENT", "r"))


def test_reply_resolve_request_review_commands():
    runner = (
        FakeRunner()
        .on("gh", "api", "repos/o/r/pulls/7/comments/101/replies", "--method", "POST", "-f", "body=@mention me", stdout="{}")
        .on("gh", "api", "graphql", "-f", f"query={RESOLVE_MUTATION}", "-f", "id=T1", stdout="{}")
        .on("gh", "api", "repos/o/r/pulls/7/requested_reviewers", "--method", "POST",
            "-f", "reviewers[]=alice", "-f", "reviewers[]=bob", stdout="{}")
    )
    gh = GitHubProvider(runner)
    gh.reply(REF, THREAD, "@mention me")
    gh.resolve(THREAD)
    gh.request_review(REF, ["alice", "bob"])
    assert len(runner.calls) == 3


def test_request_review_with_nobody_is_a_noop():
    runner = FakeRunner()
    GitHubProvider(runner).request_review(REF, [])
    assert runner.calls == []


def test_add_label_issues_one_gh_call():
    runner = FakeRunner().on("gh", "api", "repos/o/r/issues/7/labels", "--method", "POST", "-f", "labels[]=code-check", stdout="[]")
    GitHubProvider(runner).add_label(REF, "code-check")
    assert len(runner.calls) == 1


@pytest.mark.parametrize("bad", ["--foo", "-x", "a\nb", "x" * 51, ""])
def test_add_label_rejects_option_like_and_overlong_names(bad):
    runner = FakeRunner()
    with pytest.raises(ProviderError):
        GitHubProvider(runner).add_label(REF, bad)
    assert runner.calls == []


@pytest.mark.parametrize("bad", ["../..", "o/..", "./r", "-x/r", "o/r/x", ""])
def test_write_side_rejects_bad_repo_including_dot_segments(bad):
    gh = GitHubProvider(FakeRunner())
    bad_ref = PRRef(bad, 7)
    with pytest.raises(ProviderError):
        gh.post_review(bad_ref, "abc", "s", [], Decision("COMMENT", "r"))
    with pytest.raises(ProviderError):
        gh.reply(bad_ref, THREAD, "x")
    with pytest.raises(ProviderError):
        gh.request_review(bad_ref, ["alice"])


def test_write_side_rejects_bad_numbers_ids_users_and_shas():
    runner = FakeRunner()
    gh = GitHubProvider(runner)
    with pytest.raises(ProviderError):
        gh.post_review(PRRef("o/r", "--web"), "abc", "s", [], Decision("COMMENT", "r"))  # type: ignore[arg-type]
    with pytest.raises(ProviderError):
        gh.post_review(REF, "--evil", "s", [], Decision("COMMENT", "r"))
    with pytest.raises(ProviderError):
        gh.reply(PRRef("o/r", "1/../2"), THREAD, "x")  # type: ignore[arg-type]
    odd = Thread("T1", "a.py", 3, False, False, (ThreadComment("1/../x", "a", "b", "t"),))  # type: ignore[arg-type]
    with pytest.raises(ProviderError):
        gh.reply(REF, odd, "x")
    for users in (["-x"], ["a/b"], ["ok", "--flag"], [""]):
        with pytest.raises(ProviderError):
            gh.request_review(REF, users)
    assert runner.calls == []


def test_dash_leading_bodies_are_data_not_options():
    runner = (
        FakeRunner()
        .on("gh", "api", "repos/o/r/pulls/7/comments/101/replies", "--method", "POST", "-f", "body=--evil", stdout="{}")
        .on(*REVIEWS, stdout="{}")
    )
    gh = GitHubProvider(runner)
    gh.reply(REF, THREAD, "--evil")
    gh.post_review(REF, "abc", "--body", [ReviewComment("--x", 1, "RIGHT", "-y")], Decision("COMMENT", "r"))
    assert json.loads(runner.calls[1].input)["comments"][0]["path"] == "--x"


@pytest.mark.parametrize("stderr", ["HTTP 401: Bad credentials", "HTTP 502: Bad Gateway", "error connecting to api.github.com"])
def test_non_anchor_failures_with_inline_comments_raise_without_a_body_only_retry(stderr):
    runner = FakeRunner().on(*REVIEWS, returncode=1, stderr=stderr)
    with pytest.raises(ProviderError, match="posting review failed"):
        GitHubProvider(runner).post_review(REF, "abc", "s", [ReviewComment("a.py", 1, "RIGHT", "x")], Decision("COMMENT", "r"))
    assert len(runner.calls) == 1
