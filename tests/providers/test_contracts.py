
from fakes import FakeProvider, FakeRunner

from codecheck.models import PRRef, Review
from codecheck.providers.github import _ISSUE_COMMENTS_JQ, _REVIEWS_JQ, GitHubProvider
from providers.contract import ProviderContract
from providers.test_github_read import CHECKS_JSON, PR_JSON, node, page


class TestFakeProviderContract(ProviderContract):
    def make_provider(self):
        from codecheck.models import Check, Thread, ThreadComment

        fake = FakeProvider()
        pr = fake.add_pr()
        fake.files[pr.ref] = ["a.py"]
        fake.threads[pr.ref] = [Thread("T1", "a.py", 3, False, False, (ThreadComment(1, "alice", "x", "t"),))]
        fake.reviews[pr.ref] = [Review("me", "abc", "2026-01-01T00:00:00Z", "COMMENTED")]
        fake.checks[pr.ref] = [Check("ci", "pass")]
        return fake, pr.ref


class TestGitHubContract(ProviderContract):
    def make_provider(self):
        runner = (
            FakeRunner()
            .on_prefix("gh", "api", "user", stdout="me\n")
            .on_prefix("gh", "pr", "view", stdout=PR_JSON)
            .on_prefix("gh", "pr", "diff", stdout="a.py\n")
            .on_prefix("gh", "pr", "checks", stdout=CHECKS_JSON, returncode=1)
            .on_prefix("gh", "api", "graphql", stdout=page([node("T1")]))
            .on("gh", "api", "repos/o/r/pulls/7/reviews", "--paginate", "--jq", _REVIEWS_JQ,
                stdout='{"author": "me", "commit_id": "abc", "submitted_at": "2026-01-01T00:00:00Z", "state": "COMMENTED"}\n')
            .on("gh", "api", "repos/o/r/issues/7/comments", "--paginate", "--jq", _ISSUE_COMMENTS_JQ, stdout="")
            .on_prefix("gh", "api", "repos/o/r/pulls/7/reviews", stdout="{}")
            .on_prefix("gh", "api", "repos/o/r/pulls/7/comments/101/replies", stdout="{}")
            .on_prefix("gh", "api", "repos/o/r/pulls/7/requested_reviewers", stdout="{}")
            .on_prefix("gh", "api", "repos/o/r/issues/7/labels", stdout="[]")
        )
        return GitHubProvider(runner), PRRef("o/r", 7)
