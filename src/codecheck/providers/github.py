"""GitHub adapter built on the `gh` CLI. No MCP, no stored credentials."""

import json
import re
from collections.abc import Sequence
from pathlib import Path

from codecheck.errors import ProviderError
from codecheck.models import (
    Check,
    Decision,
    PostResult,
    PRRef,
    PullRequest,
    ReviewComment,
    Thread,
    ThreadComment,
)
from codecheck.runner import CommandResult, CommandRunner

_REMOTE = re.compile(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$")
_RUN_ID = re.compile(r"/actions/runs/(\d+)")
_BUCKET = {"pass": "pass", "fail": "fail", "pending": "pending", "skipping": "skipped", "cancel": "cancelled"}
_SCOPES = {"review-requested": "--review-requested=@me", "authored": "--author=@me"}
_PR_FIELDS = "number,title,body,author,baseRefName,headRefName,headRefOid,isDraft,state,url,isCrossRepository"
_REPO = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.-]*/[A-Za-z0-9_.][A-Za-z0-9_.-]*")
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\[bot\])?")
_LABEL = re.compile(r"[A-Za-z0-9._ :-]{1,50}")
_SHA = re.compile(r"[0-9A-Fa-f]{1,64}")
_DIGITS = re.compile(r"[0-9]+")
_LOG_TAIL_CHARS = 20_000

# GitHub rejects a query whose requested nodes (the product of the nested `first:` values) exceed 500,000.
THREADS_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!,$after:String){"
    "repository(owner:$owner,name:$name){pullRequest(number:$number){"
    "reviewThreads(first:50,after:$after){pageInfo{hasNextPage endCursor} "
    "nodes{id isResolved isOutdated path line originalLine "
    "comments(first:100){nodes{databaseId author{login} body createdAt reactions(first:20){nodes{content}}}}}}}}}"
)
RESOLVE_MUTATION = "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}"


def parse_remote_url(url: str) -> str | None:
    m = _REMOTE.search(url.strip())
    return m.group(1) if m else None


def _repo(repo: str) -> str:
    """Reject anything that is not owner/name; in particular a leading '-' can never reach gh as an option."""
    if not isinstance(repo, str) or not _REPO.fullmatch(repo):
        raise ProviderError(f"invalid repository {repo!r}; expected owner/name")
    if any(seg.strip(".") == "" for seg in repo.split("/")):
        raise ProviderError(f"invalid repository {repo!r}; dot-only segments are not allowed")
    return repo


def _num(number: int) -> str:
    text = str(number)
    if isinstance(number, bool) or not _DIGITS.fullmatch(text):
        raise ProviderError(f"invalid PR number {number!r}")
    return text


def _login(user: str) -> str:
    if not isinstance(user, str) or not _LOGIN.fullmatch(user):
        raise ProviderError(f"invalid user login {user!r}")
    return user


def _label(label: str) -> str:
    if not isinstance(label, str) or label.startswith("-") or not _LABEL.fullmatch(label):
        raise ProviderError(f"invalid label {label!r}")
    return label


def _sha(sha: str) -> str:
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise ProviderError(f"invalid commit id {sha!r}")
    return sha


def _thread(n: dict) -> Thread:
    comments = tuple(
        ThreadComment(
            id=c["databaseId"],
            author=(c.get("author") or {}).get("login", "ghost"),
            body=c.get("body") or "",
            created_at=c.get("createdAt") or "",
            reactions=tuple(r["content"] for r in (c.get("reactions") or {}).get("nodes", [])),
        )
        for c in n["comments"]["nodes"]
    )
    return Thread(n["id"], n["path"], n.get("line") or n.get("originalLine"), n["isResolved"], n["isOutdated"], comments)


_ANCHOR_REJECTED = re.compile(r"\b422\b|unprocessable|could not be resolved|pull_request_review_thread", re.IGNORECASE)


def _anchor_rejected(stderr: str) -> bool:
    return bool(_ANCHOR_REJECTED.search(stderr or ""))


class GitHubProvider:
    name = "github"

    def __init__(self, runner: CommandRunner, cwd: Path | None = None) -> None:
        self.runner = runner
        self.cwd = cwd

    def _gh(self, *args: str, input: str | None = None, ok: Sequence[int] = (0,)) -> CommandResult:
        res = self.runner.run(["gh", *args], input=input, cwd=self.cwd)
        if res.returncode not in ok:
            raise ProviderError(f"gh {' '.join(args[:3])} failed ({res.returncode}): {res.stderr.strip()[:500]}")
        return res

    def _json(self, *args: str):
        return json.loads(self._gh(*args).stdout or "null")

    def whoami(self) -> str:
        return self._gh("api", "user", "--jq", ".login").stdout.strip()

    def get_pr(self, ref: PRRef) -> PullRequest:
        d = self._json("pr", "view", _num(ref.number), "--repo", _repo(ref.repo), "--json", _PR_FIELDS)
        return PullRequest(
            ref=ref, title=d["title"], body=d.get("body") or "", author=(d.get("author") or {}).get("login", "ghost"),
            base_ref=d["baseRefName"], head_ref=d["headRefName"], head_sha=d["headRefOid"],
            is_draft=bool(d["isDraft"]), state=d["state"].lower(), url=d["url"],
            is_cross_repo=bool(d.get("isCrossRepository", False)),
        )

    def list_prs(self, scope: str, repos: Sequence[str]) -> list[PRRef]:
        if scope not in _SCOPES:
            raise ProviderError(f"unknown scope {scope!r}; use one of {sorted(_SCOPES)}")
        args = ["search", "prs", "--state", "open", _SCOPES[scope], "--limit", "50", "--json", "number,repository"]
        for repo in repos:
            args += ["--repo", _repo(repo)]
        return [PRRef(i["repository"]["nameWithOwner"], i["number"]) for i in self._json(*args)]

    def list_changed_files(self, ref: PRRef) -> list[str]:
        out = self._gh("pr", "diff", _num(ref.number), "--repo", _repo(ref.repo), "--name-only").stdout
        return [line for line in out.split("\n") if line]

    def fetch_refspec(self, ref: PRRef) -> tuple[str, str]:
        n = _num(ref.number)
        local = f"refs/code-check/pr/{n}"
        return f"+pull/{n}/head:{local}", local

    def repo_from_remote(self, url: str) -> str | None:
        return parse_remote_url(url)

    def clone_argv(self, repo: str, dest: str) -> list[str]:
        if dest.startswith("-"):
            raise ProviderError(f"invalid clone destination {dest!r}")
        return ["gh", "repo", "clone", _repo(repo), dest]

    def list_threads(self, ref: PRRef) -> list[Thread]:
        owner, name = _repo(ref.repo).split("/", 1)
        threads: list[Thread] = []
        after: str | None = None
        while True:
            args = ["api", "graphql", "-f", f"query={THREADS_QUERY}", "-f", f"owner={owner}", "-f", f"name={name}",
                    "-F", f"number={_num(ref.number)}"]
            if after:
                args += ["-f", f"after={after}"]
            block = self._json(*args)["data"]["repository"]["pullRequest"]["reviewThreads"]
            threads += [_thread(n) for n in block["nodes"]]
            if not block["pageInfo"]["hasNextPage"]:
                return threads
            after = block["pageInfo"]["endCursor"]

    def get_checks(self, ref: PRRef) -> list[Check]:
        res = self._gh("pr", "checks", _num(ref.number), "--repo", _repo(ref.repo), "--json", "name,bucket,link", ok=(0, 1, 8))
        if not res.stdout.strip():
            if res.returncode != 0 and "no checks reported" not in res.stderr:
                raise ProviderError(f"gh pr checks failed ({res.returncode}): {res.stderr.strip()[:500]}")
            return []
        checks = []
        for c in json.loads(res.stdout):
            m = _RUN_ID.search(c.get("link") or "")
            checks.append(Check(c["name"], _BUCKET.get(c.get("bucket"), "pending"), c.get("link") or "", m.group(1) if m else None))
        return checks

    def get_check_log(self, ref: PRRef, check: Check) -> str:
        if not check.run_id:
            return ""
        if not _DIGITS.fullmatch(check.run_id):
            raise ProviderError(f"invalid run id {check.run_id!r}")
        out = self._gh("run", "view", check.run_id, "--repo", _repo(ref.repo), "--log-failed").stdout
        return out[-_LOG_TAIL_CHARS:]

    def post_review(
        self, ref: PRRef, head_sha: str, body: str, comments: Sequence[ReviewComment], decision: Decision
    ) -> PostResult:
        endpoint = f"repos/{_repo(ref.repo)}/pulls/{_num(ref.number)}/reviews"
        commit_id = _sha(head_sha)
        argv = ["gh", "api", endpoint, "--method", "POST", "--input", "-"]

        def payload(text: str, inline: Sequence[ReviewComment]) -> str:
            return json.dumps({
                "commit_id": commit_id, "body": text, "event": decision.event,
                "comments": [{"path": c.path, "line": c.line, "side": c.side, "body": c.body} for c in inline],
            })

        res = self.runner.run(argv, input=payload(body, comments), cwd=self.cwd)
        if res.returncode == 0:
            return PostResult(len(comments))
        if not comments or not _anchor_rejected(res.stderr):
            # Only an unresolvable inline anchor (HTTP 422) is worth retrying as a body-only review;
            # auth, network or server errors would fail the retry too, or hide a real problem.
            raise ProviderError(f"posting review failed ({res.returncode}): {res.stderr.strip()[:500]}")
        folded = body + "\n\n---\n_The host rejected the inline anchors, so the findings are listed here instead._\n\n"
        folded += "\n\n".join(f"**`{c.path}:{c.line}`**\n{c.body}" for c in comments)
        retry = self.runner.run(argv, input=payload(folded, []), cwd=self.cwd)
        if retry.returncode != 0:
            raise ProviderError(f"posting review failed ({retry.returncode}): {retry.stderr.strip()[:500]}")
        return PostResult(0, degraded=True)

    def reply(self, ref: PRRef, thread: Thread, body: str) -> None:
        root = _num(thread.comments[0].id)
        self._gh("api", f"repos/{_repo(ref.repo)}/pulls/{_num(ref.number)}/comments/{root}/replies",
                 "--method", "POST", "-f", f"body={body}")

    def resolve(self, thread: Thread) -> None:
        self._gh("api", "graphql", "-f", f"query={RESOLVE_MUTATION}", "-f", f"id={thread.id}")

    def add_label(self, ref: PRRef, label: str) -> None:
        # Adding a label that is already on the PR is a no-op on GitHub; a missing label is created.
        self._gh("api", f"repos/{_repo(ref.repo)}/issues/{_num(ref.number)}/labels", "--method", "POST",
                 "-f", f"labels[]={_label(label)}")

    def request_review(self, ref: PRRef, users: Sequence[str]) -> None:
        if not users:
            return
        args = ["api", f"repos/{_repo(ref.repo)}/pulls/{_num(ref.number)}/requested_reviewers", "--method", "POST"]
        for user in users:
            args += ["-f", f"reviewers[]={_login(user)}"]
        self._gh(*args)
