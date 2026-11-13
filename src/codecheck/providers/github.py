"""GitHub adapter built on the `gh` CLI. No MCP, no stored credentials."""

import json
import re
from collections.abc import Sequence
from pathlib import Path

from codecheck.errors import ProviderError
from codecheck.models import Check, PRRef, PullRequest, Thread, ThreadComment
from codecheck.runner import CommandResult, CommandRunner

_REMOTE = re.compile(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$")
_RUN_ID = re.compile(r"/actions/runs/(\d+)")
_BUCKET = {"pass": "pass", "fail": "fail", "pending": "pending", "skipping": "skipped", "cancel": "cancelled"}
_SCOPES = {"review-requested": "--review-requested=@me", "authored": "--author=@me"}
_PR_FIELDS = "number,title,body,author,baseRefName,headRefName,headRefOid,isDraft,state,url,isCrossRepository"
_REPO = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.-]*/[A-Za-z0-9_.][A-Za-z0-9_.-]*")
_DIGITS = re.compile(r"[0-9]+")
_LOG_TAIL_CHARS = 20_000

THREADS_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!,$after:String){"
    "repository(owner:$owner,name:$name){pullRequest(number:$number){"
    "reviewThreads(first:100,after:$after){pageInfo{hasNextPage endCursor} "
    "nodes{id isResolved isOutdated path line originalLine "
    "comments(first:100){nodes{databaseId author{login} body createdAt reactions(first:50){nodes{content}}}}}}}}}"
)
RESOLVE_MUTATION = "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}"


def parse_remote_url(url: str) -> str | None:
    m = _REMOTE.search(url.strip())
    return m.group(1) if m else None


def _repo(repo: str) -> str:
    """Reject anything that is not owner/name; in particular a leading '-' can never reach gh as an option."""
    if not isinstance(repo, str) or not _REPO.fullmatch(repo):
        raise ProviderError(f"invalid repository {repo!r}; expected owner/name")
    return repo


def _num(number: int) -> str:
    text = str(number)
    if isinstance(number, bool) or not _DIGITS.fullmatch(text):
        raise ProviderError(f"invalid PR number {number!r}")
    return text


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
