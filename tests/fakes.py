"""Test doubles: FakeRunner (scripted commands) and, later, FakeProvider."""

from dataclasses import dataclass
from pathlib import Path

from codecheck.runner import CommandResult


@dataclass
class Call:
    argv: tuple[str, ...]
    input: str | None
    cwd: Path | None


class FakeRunner:
    def __init__(self) -> None:
        self._exact: dict[tuple[str, ...], list[CommandResult]] = {}
        self._prefix: list[tuple[tuple[str, ...], list[CommandResult]]] = []
        self.calls: list[Call] = []

    def on(self, *argv: str, stdout: str = "", stderr: str = "", returncode: int = 0) -> FakeRunner:
        self._exact.setdefault(tuple(argv), []).append(CommandResult(returncode, stdout, stderr))
        return self

    def on_prefix(self, *prefix: str, stdout: str = "", stderr: str = "", returncode: int = 0) -> FakeRunner:
        self._prefix.append((tuple(prefix), [CommandResult(returncode, stdout, stderr)]))
        return self

    def run(self, args, *, input=None, cwd=None, timeout=None) -> CommandResult:
        argv = tuple(args)
        self.calls.append(Call(argv, input, cwd))
        queue = self._exact.get(argv)
        if queue is None:
            for prefix, q in self._prefix:
                if argv[: len(prefix)] == prefix:
                    queue = q
                    break
        if queue is None:
            raise AssertionError(f"unexpected command: {argv}")
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def argvs(self) -> list[tuple[str, ...]]:
        return [c.argv for c in self.calls]


from codecheck.errors import ProviderError
from codecheck.models import (
    Check,
    Decision,
    PostResult,
    PRRef,
    PullRequest,
    Review,
    Thread,
)


class FakeProvider:
    """In-memory Provider for CLI tests and the contract suite."""

    name = "fake"

    def __init__(self, repo: str = "o/r") -> None:
        self.repo = repo
        self.me = "me"
        self.prs: dict[PRRef, PullRequest] = {}
        self.files: dict[PRRef, list[str]] = {}
        self.listing: dict[str, list[PRRef]] = {"review-requested": [], "authored": []}
        self.threads: dict[PRRef, list[Thread]] = {}
        self.checks: dict[PRRef, list[Check]] = {}
        self.reviews: dict[PRRef, list[Review]] = {}
        self.thread_calls: list = []
        self.review_calls: list = []
        self.logs: dict[str, str] = {}
        self.posted: list[dict] = []
        self.replies: list[tuple[PRRef, str, str]] = []
        self.resolved: list[str] = []
        self.requested: list[tuple[PRRef, list[str]]] = []
        self.labeled: list[tuple[PRRef, str]] = []
        self.label_error = False
        self.private: bool | None = None
        self.private_calls: list = []

    def add_pr(self, number: int = 7, **over) -> PullRequest:
        ref = PRRef(self.repo, number)
        base = {"ref": ref, "title": "t", "body": "b", "author": "alice", "base_ref": "main", "head_ref": "feature",
                    "head_sha": "abc123", "is_draft": False, "state": "open", "url": "u", "is_cross_repo": False}
        pr = PullRequest(**{**base, **over})
        self.prs[ref] = pr
        return pr

    def whoami(self) -> str:
        return self.me

    def list_prs(self, scope, repos):
        return list(self.listing[scope])

    def get_pr(self, ref):
        return self.prs[ref]

    def list_changed_files(self, ref):
        return list(self.files.get(ref, []))

    def fetch_refspec(self, ref):
        head = self.prs[ref].head_ref
        return (f"+refs/heads/{head}:refs/code-check/pr/{ref.number}", f"refs/code-check/pr/{ref.number}")

    def repo_from_remote(self, url):
        return self.repo

    def repo_is_private(self, repo):
        self.private_calls.append(repo)
        return self.private

    def clone_argv(self, repo, dest):
        return ["git", "clone", f"file:///fake/{repo}", dest]

    def list_threads(self, ref, reactions=True):
        self.thread_calls.append((ref, reactions))
        return list(self.threads.get(ref, []))

    def list_reviews(self, ref):
        self.review_calls.append(ref)
        return list(self.reviews.get(ref, []))

    def post_review(self, ref, head_sha, body, comments, decision: Decision):
        self.posted.append({"ref": ref, "head_sha": head_sha, "body": body, "comments": list(comments), "decision": decision})
        return PostResult(len(comments))

    def reply(self, ref, thread, body):
        self.replies.append((ref, thread.id, body))

    def resolve(self, thread):
        self.resolved.append(thread.id)

    def request_review(self, ref, users):
        self.requested.append((ref, list(users)))

    def add_label(self, ref, label):
        if self.label_error:
            raise ProviderError("cannot label")
        self.labeled.append((ref, label))

    def get_checks(self, ref):
        return list(self.checks.get(ref, []))

    def get_check_log(self, ref, check):
        return self.logs.get(check.name, "")


class HybridRunner:
    """Real git, scripted everything else."""

    def __init__(self, fake: FakeRunner) -> None:
        from codecheck.runner import SubprocessRunner

        self.fake = fake
        self.real = SubprocessRunner()

    def run(self, args, *, input=None, cwd=None, timeout=None):
        target = self.real if args and args[0] == "git" else self.fake
        return target.run(args, input=input, cwd=cwd, timeout=timeout)
