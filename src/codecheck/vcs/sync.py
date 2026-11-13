"""Fetch the latest remote base branch so comparisons use current upstream."""

import datetime
from dataclasses import dataclass

from codecheck.errors import SyncError
from codecheck.vcs.git import Git


@dataclass(frozen=True, slots=True)
class SyncResult:
    base_ref: str
    base_sha: str
    fresh: bool
    tip_time: int
    warning: str | None = None


def sync_base(git: Git, remote: str, base_branch: str = "auto") -> SyncResult:
    if remote.startswith("-") or base_branch.startswith("-"):
        raise SyncError(f"refusing remote/base_branch that starts with '-': {remote!r} {base_branch!r}")
    branch = git.default_branch(remote) if base_branch == "auto" else base_branch
    fresh = git.fetch(remote, branch)
    ref = f"{remote}/{branch}"
    if not git.ref_exists(ref):
        raise SyncError(f"base ref {ref} not found (fetch {'succeeded' if fresh else 'failed'})")
    sha = git.rev_parse(ref)
    tip = git.commit_time(ref)
    warning = None
    if not fresh:
        when = datetime.datetime.fromtimestamp(tip, datetime.UTC).strftime("%Y-%m-%d %H:%M UTC")
        warning = f"fetch of {ref} failed; using last-fetched {sha[:8]} (tip dated {when}); base may be stale"
    return SyncResult(ref, sha, fresh, tip, warning)
