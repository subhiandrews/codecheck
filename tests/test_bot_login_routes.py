"""Every place that asks "is this comment ours?" must accept our bot login in both of GitHub's spellings.

`review.login` (and REST) say `github-actions[bot]`; GraphQL, which supplies thread comments, says `github-actions`.
A person who happens to be called `github-actions` is not us."""

from fakes import FakeProvider
from test_shadow_collect import REF, RID, seed

from codecheck.commands.post import _already_posted
from codecheck.commands.reply import MARKER as REPLY_MARKER
from codecheck.commands.threads import _reviewers
from codecheck.core.precheck import cheap_skip, review_activity
from codecheck.core.render import RUN_MARKER, marker
from codecheck.core.rounds import our_reviews
from codecheck.core.state import actionable_threads
from codecheck.models import (
    OWN_MARKER,
    PRRef,
    PullRequest,
    Review,
    Thread,
    ThreadComment,
    comment_is_ours,
    same_pr_author,
)
from codecheck.shadow.collect import collect_outcomes
from codecheck.shadow.store import ShadowStore

ME = "github-actions[bot]"
MARK = "**Warning**\n<!-- code-check:finding:aaaaaaaaaaaa -->"


def tc(cid, author, body="body", *, bot=False):
    return ThreadComment(cid, author, body, "t", (), is_bot=bot)


def thread(*comments, tid="T1"):
    return Thread(tid, "a.py", 3, False, False, tuple(comments))


def test_gh_spells_a_bot_pr_author_app_slash_name_and_that_is_still_our_own_pr():
    # `gh pr view --json author` gives `app/dependabot` for a bot, so a PR our CI bot opened is `app/github-actions`
    assert same_pr_author("app/github-actions", ME)
    assert same_pr_author(ME, ME) and same_pr_author("alice", "alice")
    assert not same_pr_author("github-actions", ME)  # a person called like the bot
    assert not same_pr_author("app/dependabot", ME)
    assert same_pr_author("app/github-actions", "github-actions")  # review.login may be the bare name
    assert not same_pr_author("alice", "")
    pr = PullRequest(PRRef("o/r", 7), "t", "", "app/github-actions", "main", "f", "abc", False, "open", "u")
    skip = cheap_skip(pr, ["a.py"], ignore=(), me=ME)
    assert skip is not None and "own PR" in skip.reason


def test_post_recognises_findings_we_already_posted_as_the_bot():
    assert _already_posted([thread(tc(1, "github-actions", MARK, bot=True))], ME) == {"a" * 12}
    assert _already_posted([thread(tc(1, "github-actions", MARK))], ME) == set()  # a person with that name
    assert _already_posted([thread(tc(1, ME, MARK, bot=True))], ME) == {"a" * 12}  # the REST spelling still works


def test_our_marker_pattern_matches_every_marker_we_write():
    # the real writers, so a changed format fails here and not silently in comment_is_ours
    assert OWN_MARKER.search(marker("a" * 12)) and OWN_MARKER.search(RUN_MARKER.format("c4141e60f383"))
    assert OWN_MARKER.search(f"a reply\n\n{REPLY_MARKER}")
    assert not OWN_MARKER.search("<!-- code-check:finding:xyz -->")


def test_reviews_the_bot_posted_are_ours_whichever_way_review_login_is_spelled():
    # REST says github-actions[bot]; review.login may be that or the bare name
    run = Review(ME, "abc", "2026-01-01T00:00:10Z", "COMMENTED", RUN_MARKER.format("c4141e60f383"), True)
    other_workflow = Review(ME, "def", "2026-01-02T00:00:10Z", "COMMENTED", "coverage dropped", True)
    quoting = Review(ME, "jkl", "2026-01-04T00:00:10Z", "COMMENTED", f"quoted: {RUN_MARKER.format('c4141e60f383')}\nmore", True)
    for me in (ME, "github-actions"):
        assert our_reviews([run, other_workflow, quoting], me) == [run]  # one rule for rounds and for activity
        assert review_activity([quoting], [], me).last_review is None
        assert review_activity([run, other_workflow], [], me).last_review == run
    person = Review("alice", "ghi", "2026-01-03T00:00:10Z", "COMMENTED", "my own review")
    assert review_activity([person], [], "alice").last_review == person  # a person's reviews need no marker


def test_a_comment_from_the_shared_bot_account_is_ours_only_with_our_marker():
    # every workflow using GITHUB_TOKEN posts as github-actions: another workflow's comment is not ours
    ours = tc(1, "github-actions", MARK, bot=True)
    other = tc(2, "github-actions", "coverage dropped by 2%", bot=True)
    person = tc(3, "github-actions", MARK)
    assert comment_is_ours(ours, ME) and not comment_is_ours(other, ME) and not comment_is_ours(person, ME)
    assert not comment_is_ours(tc(4, ME, "an exact login is no reason to trust a bot's text", bot=True), ME)
    assert comment_is_ours(tc(5, "alice", "no marker on a person's own comment"), "alice")
    # review.login may also be the bare name (config allows it): a bot comment still needs our marker
    assert comment_is_ours(ours, "github-actions") and not comment_is_ours(other, "github-actions")
    # a whole marker, not just the text that starts one
    assert not comment_is_ours(tc(6, "github-actions", "<!-- code-check: nothing -->", bot=True), ME)
    # and where we write it, at the end: a marker quoted mid-body by another workflow does not make a comment ours
    quoted = tc(7, "github-actions", f"a bot quoting a PR:\n{MARK}\nand more text", bot=True)
    assert not comment_is_ours(quoted, ME) and _already_posted([thread(quoted)], ME) == set()
    assert _already_posted([thread(other, ours, tid="x")], ME) == {"a" * 12}
    assert [t.id for t in actionable_threads([thread(tc(5, "alice"), other, tid="o")], ME)] == ["o"]


def test_our_own_last_word_is_not_actionable():
    ours = thread(tc(1, "alice"), tc(2, "github-actions", MARK, bot=True), tid="ours")
    theirs = thread(tc(3, "github-actions"), tid="theirs")  # a person named like the bot
    assert [t.id for t in actionable_threads([ours, theirs], ME)] == ["theirs"]


def test_a_bot_is_never_a_reviewer_to_ask_again():
    threads = [thread(tc(1, "github-actions", bot=True), tid="A"), thread(tc(2, "alice"), tc(3, "ci[bot]"), tid="B")]
    assert _reviewers(threads, "me") == ["alice"]


def test_collect_attributes_threads_the_bot_opened_to_our_findings(tmp_path):
    shadow = ShadowStore(tmp_path)
    seed(shadow)
    provider = FakeProvider()
    provider.threads[REF] = [Thread("T1", "a.py", 3, True, False, (tc(1, "github-actions", MARK, bot=True),))]
    summary = collect_outcomes(provider, shadow, REF, ME)
    assert summary["outcomes"] == 1 and summary["human_comments"] == 0
    assert next(r for r in shadow.read(RID) if r["type"] == "outcome")["finding_id"] == "a" * 12
