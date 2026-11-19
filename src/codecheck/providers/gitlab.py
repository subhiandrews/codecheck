"""GitLab adapter built on the `glab` CLI (`glab api`, so the REST contract is stable across glab versions).

Differences from GitHub that shape this file:
- Projects can be nested (`group/subgroup/project`), so `nested_repos` is true and the path is URL-encoded.
- There is no review object. A review is the inline discussions plus one summary note that carries a hidden
  marker with the reviewed head SHA; `list_reviews` finds our past reviews through that marker.
- A thread id has to carry its project and merge request (`resolve` receives no ref): `project:iid:discussion`.
- Reactions (award emoji) cost one call per note, so threads come back without them.
"""

import json
import re
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import quote

from codecheck.errors import ProviderError
from codecheck.models import (
    Check,
    Decision,
    IssueComment,
    PostResult,
    PRRef,
    PullRequest,
    Review,
    ReviewComment,
    Thread,
    ThreadComment,
    is_service_login,
)
from codecheck.runner import CommandResult, CommandRunner

_REMOTE_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://(?:[^/@]+@)?([^/:]+)(?::\d+)?/(.+?)(?:\.git)?/?$")
_REMOTE_SCP = re.compile(r"^(?:[^@/]+@)?([^/:@]+):(?!//)(.+?)(?:\.git)?/?$")
_SEGMENT = r"[A-Za-z0-9_.][A-Za-z0-9_.-]*"
_REPO = re.compile(rf"{_SEGMENT}(?:/{_SEGMENT})+")
_LOGIN = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.-]{0,254}")
_LABEL = re.compile(r"[A-Za-z0-9._ :-]{1,50}")
_SHA = re.compile(r"[0-9A-Fa-f]{1,64}")
_DIGITS = re.compile(r"[0-9]+")
_DISCUSSION = re.compile(r"[0-9a-f]{8,64}")
_MARKER = re.compile(r"<!-- code-check:review:([0-9a-f]{1,64}) -->")
_LOG_TAIL_CHARS = 20_000
# System notes GitLab writes when someone approves or withdraws an approval.
_VOTES = {"approved this merge request": "APPROVED", "unapproved this merge request": "DISMISSED"}
_STATE = {"opened": "open", "merged": "merged", "closed": "closed", "locked": "closed"}
_JOB_STATE = {
    "success": "pass", "failed": "fail", "canceled": "cancelled", "canceling": "cancelled", "skipped": "skipped",
    "manual": "skipped", "running": "pending", "pending": "pending", "created": "pending", "preparing": "pending",
    "waiting_for_resource": "pending", "scheduled": "pending",
}
_SCOPES = {"review-requested": "reviewer_username", "authored": "author_username"}
_POSITION_REJECTED = re.compile(r"\b400\b|\b422\b|line_code|position|diff_refs|could not be resolved", re.IGNORECASE)


def parse_remote_url(url: str) -> str | None:
    """`https://gitlab.com/a/b/c.git` or `git@gitlab.com:a/b/c.git` to `a/b/c`; None unless it has a namespace and a name."""
    m = _REMOTE_URL.match(url.strip()) or _REMOTE_SCP.match(url.strip())
    if not m:
        return None
    path = m.group(2)
    return path if _REPO.fullmatch(path) else None


def remote_host(url: str) -> str | None:
    m = _REMOTE_URL.match(url.strip()) or _REMOTE_SCP.match(url.strip())
    return m.group(1).lower() if m else None


def _repo(repo: str) -> str:
    """Reject anything that is not namespace/project; a leading '-' can never reach glab as an option."""
    if not isinstance(repo, str) or not _REPO.fullmatch(repo):
        raise ProviderError(f"invalid repository {repo!r}; expected group/project")
    if any(seg.strip(".") == "" for seg in repo.split("/")):
        raise ProviderError(f"invalid repository {repo!r}; dot-only segments are not allowed")
    return repo


def _pid(repo: str) -> str:
    return quote(_repo(repo), safe="")


def _num(number: int) -> str:
    text = str(number)
    if isinstance(number, bool) or not _DIGITS.fullmatch(text):
        raise ProviderError(f"invalid merge request number {number!r}")
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


def _docs(text: str) -> list:
    """Every JSON document in `text`, flattened: `glab api --paginate` may print one array per page."""
    decoder = json.JSONDecoder()
    out: list = []
    i = 0
    while i < len(text):
        if text[i].isspace():
            i += 1
            continue
        doc, i = decoder.raw_decode(text, i)
        if isinstance(doc, list):
            out.extend(doc)
        else:
            out.append(doc)
    return out


def _author(note: dict) -> str:
    return (note.get("author") or {}).get("username") or "ghost"


def _is_bot(note: dict) -> bool:
    author = note.get("author") or {}
    return bool(author.get("bot")) or is_service_login(author.get("username") or "")


def thread_id(repo: str, number: int, discussion: str) -> str:
    return f"{repo}:{number}:{discussion}"


def split_thread_id(tid: str) -> tuple[str, int, str]:
    repo, _, rest = tid.partition(":")
    number, _, discussion = rest.partition(":")
    if not _REPO.fullmatch(repo) or not _DIGITS.fullmatch(number) or not _DISCUSSION.fullmatch(discussion):
        raise ProviderError(f"invalid thread id {tid!r}")
    return repo, int(number), discussion


class GitLabProvider:
    name = "gitlab"
    nested_repos = True

    def __init__(self, runner: CommandRunner, cwd: Path | None = None, login: str = "") -> None:
        self.runner = runner
        self.cwd = cwd
        self.login = login

    def _glab(self, argv: Sequence[str], *, input: str | None = None, ok: Sequence[int] = (0,)) -> CommandResult:
        res = self.runner.run(["glab", *argv], input=input, cwd=self.cwd)
        if res.returncode not in ok:
            raise ProviderError(f"glab {' '.join(argv[:3])} failed ({res.returncode}): {(res.stderr or res.stdout).strip()[:500]}")
        return res

    def _get(self, endpoint: str, *, paginate: bool = False):
        res = self._glab(["api", endpoint] + (["--paginate"] if paginate else []))
        return _docs(res.stdout) if paginate else json.loads(res.stdout or "null")

    def _send(self, endpoint: str, method: str, body: dict) -> CommandResult:
        argv = ["api", endpoint, "--method", method, "--header", "Content-Type: application/json", "--input", "-"]
        return self._glab(argv, input=json.dumps(body))

    def _mr(self, ref: PRRef) -> str:
        return f"projects/{_pid(ref.repo)}/merge_requests/{_num(ref.number)}"

    def whoami(self) -> str:
        return self.login or self._get("user")["username"]

    def get_pr(self, ref: PRRef) -> PullRequest:
        d = self._get(self._mr(ref))
        return PullRequest(
            ref=ref, title=d["title"], body=d.get("description") or "", author=_author(d),
            base_ref=d["target_branch"], head_ref=d["source_branch"], head_sha=d.get("sha") or "",
            is_draft=bool(d.get("draft") or d.get("work_in_progress")), state=_STATE.get(d["state"], "closed"),
            url=d["web_url"], is_cross_repo=d.get("source_project_id") != d.get("target_project_id"),
        )

    def list_prs(self, scope: str, repos: Sequence[str]) -> list[PRRef]:
        if scope not in _SCOPES:
            raise ProviderError(f"unknown scope {scope!r}; use one of {sorted(_SCOPES)}")
        query = f"state=opened&scope=all&per_page=50&{_SCOPES[scope]}={quote(_login(self.whoami()), safe='')}"
        endpoints = [f"projects/{_pid(r)}/merge_requests?{query}" for r in repos] or [f"merge_requests?{query}"]
        refs: list[PRRef] = []
        for endpoint in endpoints:
            for item in self._get(endpoint):
                repo = (item.get("references") or {}).get("full", "").rpartition("!")[0]
                if repo:
                    refs.append(PRRef(repo, item["iid"]))
        return refs[:50]

    def list_changed_files(self, ref: PRRef) -> list[str]:
        diffs = self._get(f"{self._mr(ref)}/diffs?per_page=100", paginate=True)
        return [d["new_path"] for d in diffs if d.get("new_path")]

    def fetch_refspec(self, ref: PRRef) -> tuple[str, str]:
        n = _num(ref.number)
        local = f"refs/code-check/pr/{n}"
        return f"+refs/merge-requests/{n}/head:{local}", local

    def repo_from_remote(self, url: str) -> str | None:
        return parse_remote_url(url)

    def repo_is_private(self, repo: str) -> bool | None:
        try:
            visibility = self._get(f"projects/{_pid(repo)}").get("visibility")
        except (ProviderError, ValueError):
            return None
        return None if visibility is None else visibility != "public"

    def repo_license(self, repo: str) -> str | None:
        try:
            lic = self._get(f"projects/{_pid(repo)}?license=true").get("license")
        except (ProviderError, ValueError, AttributeError):
            return None
        key = lic.get("key") if isinstance(lic, dict) else None
        return key.lower() if isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9.+-]{1,64}", key) else None

    def clone_argv(self, repo: str, dest: str) -> list[str]:
        if dest.startswith("-"):
            raise ProviderError(f"invalid clone destination {dest!r}")
        return ["glab", "repo", "clone", _repo(repo), dest]

    def list_threads(self, ref: PRRef, reactions: bool = True) -> list[Thread]:
        # `reactions` is accepted for the Provider contract; GitLab award emoji need one call per note, so none are fetched.
        threads: list[Thread] = []
        for d in self._get(f"{self._mr(ref)}/discussions?per_page=100", paginate=True):
            notes = [n for n in d.get("notes", []) if not n.get("system")]
            if not notes or notes[0].get("type") != "DiffNote":
                continue
            pos = notes[0].get("position") or {}
            resolvable = [n for n in notes if n.get("resolvable")]
            comments = tuple(
                ThreadComment(id=n["id"], author=_author(n), body=n.get("body") or "", created_at=n.get("created_at") or "",
                              reactions=(), is_bot=_is_bot(n))
                for n in notes
            )
            threads.append(Thread(
                thread_id(ref.repo, ref.number, d["id"]), pos.get("new_path") or pos.get("old_path") or "",
                pos.get("new_line") or pos.get("old_line"), bool(resolvable) and all(n.get("resolved") for n in resolvable),
                False, comments,
            ))
        return threads

    def _all_notes(self, ref: PRRef) -> list[dict]:
        return self._get(f"{self._mr(ref)}/notes?sort=asc&order_by=created_at&per_page=100", paginate=True)

    def _notes(self, ref: PRRef) -> list[dict]:
        return [n for n in self._all_notes(ref) if not n.get("system")]

    def list_reviews(self, ref: PRRef) -> list[Review]:
        notes = self._all_notes(ref)
        reviews = []
        for n in notes:
            m = _MARKER.search(n.get("body") or "") if not n.get("system") else None
            if m and n.get("created_at"):
                reviews.append(Review(_author(n), m.group(1), n["created_at"], "COMMENTED", n.get("body") or "", _is_bot(n)))
        votes = [n for n in notes if n.get("system") and n.get("body") in _VOTES and n.get("created_at")]
        if votes:
            # GitLab does not say which commit an approval was for. Pin it to the head of the newest diff version that
            # existed when it was given, so an approval older than the latest push never counts for the new head.
            versions = sorted(self._get(f"{self._mr(ref)}/versions"), key=lambda v: v.get("created_at") or "")
            for n in votes:
                head = ""
                for v in versions:
                    if (v.get("created_at") or "") <= n["created_at"]:
                        head = v.get("head_commit_sha") or ""
                reviews.append(Review(_author(n), head, n["created_at"], _VOTES[n["body"]], "", _is_bot(n)))
        return reviews

    def list_issue_comments(self, ref: PRRef) -> list[IssueComment]:
        return [IssueComment(_author(n), n.get("body") or "", n.get("created_at") or "", _is_bot(n))
                for n in self._notes(ref) if n.get("type") != "DiffNote" and not _MARKER.search(n.get("body") or "")]

    def get_checks(self, ref: PRRef) -> list[Check]:
        pipelines = self._get(f"{self._mr(ref)}/pipelines")
        if not pipelines:
            return []
        jobs = self._get(f"projects/{_pid(ref.repo)}/pipelines/{_num(pipelines[0]['id'])}/jobs?per_page=100", paginate=True)
        checks = []
        for j in jobs:
            state = _JOB_STATE.get(j.get("status"), "pending")
            if state == "fail" and j.get("allow_failure"):
                state = "skipped"  # an allowed failure never blocks the merge request, so it is not worth chasing
            checks.append(Check(j["name"], state, j.get("web_url") or "", str(j["id"])))
        return checks

    def get_check_log(self, ref: PRRef, check: Check) -> str:
        if not check.run_id:
            return ""
        if not _DIGITS.fullmatch(check.run_id):
            raise ProviderError(f"invalid run id {check.run_id!r}")
        return self._glab(["api", f"projects/{_pid(ref.repo)}/jobs/{check.run_id}/trace"]).stdout[-_LOG_TAIL_CHARS:]

    def _version(self, ref: PRRef, head_sha: str) -> dict | None:
        """The diff version of this merge request whose head is `head_sha`; inline positions are anchored to it."""
        for v in self._get(f"{self._mr(ref)}/versions"):
            if (v.get("head_commit_sha") or "").lower().startswith(head_sha.lower()):
                return v
        return None

    def post_review(
        self, ref: PRRef, head_sha: str, body: str, comments: Sequence[ReviewComment], decision: Decision
    ) -> PostResult:
        head = _sha(head_sha)
        version = self._version(ref, head) if comments else None
        posted = 0
        rejected: list[ReviewComment] = [] if version is not None else list(comments)
        for c in comments if version is not None else ():
            position = {
                "position_type": "text", "base_sha": version["base_commit_sha"], "start_sha": version["start_commit_sha"],
                "head_sha": version["head_commit_sha"], "new_path": c.path, "old_path": c.path,
                "old_line" if c.side == "LEFT" else "new_line": c.line,
            }
            res = self.runner.run(
                ["glab", "api", f"{self._mr(ref)}/discussions", "--method", "POST", "--header", "Content-Type: application/json",
                 "--input", "-"],
                input=json.dumps({"body": c.body, "position": position}), cwd=self.cwd,
            )
            if res.returncode == 0:
                posted += 1
            elif _POSITION_REJECTED.search(res.stderr or res.stdout or ""):
                rejected.append(c)  # an unresolvable anchor is folded into the summary; anything else is a real error
            else:
                raise ProviderError(f"posting review failed ({res.returncode}): {(res.stderr or res.stdout).strip()[:500]}")
        text = body
        if rejected:
            why = "The host rejected the inline anchors" if version is not None else "No diff version of this merge request matches the reviewed head"
            text += f"\n\n---\n_{why}, so these findings are listed here instead._\n\n"
            text += "\n\n".join(f"**`{c.path}:{c.line}`**\n{c.body}" for c in rejected)
        if decision.event == "APPROVE":
            try:
                self._send(f"{self._mr(ref)}/approve", "POST", {"sha": head})
            except ProviderError:
                # A refused approval (permissions, approval rules, a stale head) must not hide a review that was done.
                text += "\n\n_code-check could not approve this merge request: the host refused the approval._"
        # The summary goes last: its marker tells list_reviews this head was reviewed, so a run that dies earlier is retried.
        self._send(f"{self._mr(ref)}/notes", "POST", {"body": f"{text}\n\n<!-- code-check:review:{head} -->"})
        return PostResult(posted, degraded=bool(rejected))

    def reply(self, ref: PRRef, thread: Thread, body: str) -> None:
        repo, number, discussion = split_thread_id(thread.id)
        if (repo, number) != (ref.repo, ref.number):
            raise ProviderError(f"thread {thread.id!r} does not belong to {ref}")
        self._send(f"{self._mr(ref)}/discussions/{discussion}/notes", "POST", {"body": body})

    def resolve(self, thread: Thread) -> None:
        repo, number, discussion = split_thread_id(thread.id)
        self._send(f"projects/{_pid(repo)}/merge_requests/{number}/discussions/{discussion}", "PUT", {"resolved": True})

    def add_label(self, ref: PRRef, label: str) -> None:
        self._send(self._mr(ref), "PUT", {"add_labels": _label(label)})

    def request_review(self, ref: PRRef, users: Sequence[str]) -> None:
        if not users:
            return
        names = [_login(u) for u in users]  # validate everything before the first API call
        ids = {r["id"] for r in self._get(self._mr(ref)).get("reviewers") or []}
        for user in names:
            found = self._get(f"users?username={quote(user, safe='')}")
            if not found:
                raise ProviderError(f"no GitLab user {user!r}")
            ids.add(found[0]["id"])
        self._send(self._mr(ref), "PUT", {"reviewer_ids": sorted(ids)})
