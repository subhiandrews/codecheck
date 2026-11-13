"""Decide, without any LLM call, whether there is anything worth reviewing."""

from collections.abc import Sequence
from dataclasses import dataclass

from codecheck.core.panel import filter_ignored
from codecheck.core.state import StateStore
from codecheck.models import PullRequest


@dataclass(frozen=True, slots=True)
class PrecheckResult:
    proceed: bool
    reason: str
    files: tuple[str, ...] = ()
    fingerprint: str = ""


def _skip(reason: str) -> PrecheckResult:
    return PrecheckResult(False, reason)


def precheck_pr(
    pr: PullRequest, files: Sequence[str], state: StateStore, *, ignore: Sequence[str], me: str, force: bool = False
) -> PrecheckResult:
    if pr.state != "open":
        return _skip(f"PR is {pr.state}")
    if pr.is_draft:
        return _skip("PR is a draft")
    if pr.author == me:
        return _skip("own PR: use address-comments instead")
    reviewable = filter_ignored(files, ignore)
    if not reviewable:
        return _skip("no reviewable files after ignore patterns")
    if not force and state.get(f"review:{pr.ref}") == pr.head_sha:
        return _skip(f"head {pr.head_sha[:8]} already reviewed")
    return PrecheckResult(True, "ok", tuple(reviewable), pr.head_sha)


def precheck_local(
    files: Sequence[str], fingerprint: str, state_key: str, state: StateStore, *, ignore: Sequence[str], force: bool = False
) -> PrecheckResult:
    if not files:
        return _skip("no changes against base")
    reviewable = filter_ignored(files, ignore)
    if not reviewable:
        return _skip("no reviewable files after ignore patterns")
    if not force and state.get(state_key) == fingerprint:
        return _skip("unchanged since last review")
    return PrecheckResult(True, "ok", tuple(reviewable), fingerprint)
