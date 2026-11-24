"""Every place that asks "is this comment ours?" must accept our bot login in both of GitHub's spellings.

`review.login` (and REST) say `github-actions[bot]`; GraphQL, which supplies thread comments, says `github-actions`.
A person who happens to be called `github-actions` is not us."""

from fakes import FakeProvider
from test_shadow_collect import REF, RID, seed

from codecheck.commands.post import _already_posted
from codecheck.commands.threads import _reviewers
from codecheck.core.state import actionable_threads
from codecheck.models import Thread, ThreadComment
from codecheck.shadow.collect import collect_outcomes
from codecheck.shadow.store import ShadowStore

ME = "github-actions[bot]"
MARK = "**Warning**\n<!-- code-check:finding:aaaaaaaaaaaa -->"


def tc(cid, author, body="body", *, bot=False):
    return ThreadComment(cid, author, body, "t", (), is_bot=bot)


def thread(*comments, tid="T1"):
    return Thread(tid, "a.py", 3, False, False, tuple(comments))


def test_post_recognises_findings_we_already_posted_as_the_bot():
    assert _already_posted([thread(tc(1, "github-actions", MARK, bot=True))], ME) == {"a" * 12}
    assert _already_posted([thread(tc(1, "github-actions", MARK))], ME) == set()  # a person with that name
    assert _already_posted([thread(tc(1, ME, MARK, bot=True))], ME) == {"a" * 12}  # the REST spelling still works


def test_our_own_last_word_is_not_actionable():
    ours = thread(tc(1, "alice"), tc(2, "github-actions", bot=True), tid="ours")
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
