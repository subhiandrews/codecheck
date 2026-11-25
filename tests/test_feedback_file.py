import json

import pytest
from fakes import FakeRunner

from codecheck.core.feedback.file import Filer
from codecheck.core.feedback.manifest import Manifest
from codecheck.errors import CodeCheckError

M = Manifest(product="code-check", version="1.6.0", target_repo="o/r", kinds={"k": "t"})
BODY = "hello\n<!-- feedback-fingerprint: abc123def456 -->"
LIST = ("gh", "issue", "list", "--repo", "o/r", "--state", "open", "--search", "abc123def456 in:body", "--json", "number,url,body", "--limit", "5")


def create_argv():
    return ("gh", "issue", "create", "--repo", "o/r", "--title=[code-check] boom", "--body-file", "-")


def test_creates_issue_with_body_on_stdin_after_empty_dedupe():
    r = FakeRunner().on(*LIST, stdout="[]").on(*create_argv(), stdout="https://github.com/o/r/issues/7\n")
    res = Filer(r, M).file("[code-check] boom", BODY)
    assert (res.outcome, res.url) == ("created", "https://github.com/o/r/issues/7")
    call = next(c for c in r.calls if c.argv[:3] == ("gh", "issue", "create"))
    assert call.input == BODY


def test_duplicate_adds_a_short_comment_instead_of_a_new_issue():
    r = FakeRunner().on(*LIST, stdout=json.dumps([{"number": 3, "url": "https://github.com/o/r/issues/3", "body": "x\\n" + BODY.splitlines()[-1]}]))
    r.on("gh", "issue", "comment", "3", "--repo", "o/r", "--body-file", "-")
    res = Filer(r, M).file("[code-check] boom", BODY)
    assert (res.outcome, res.url) == ("duplicate", "https://github.com/o/r/issues/3")
    comment = next(c for c in r.calls if c.argv[:3] == ("gh", "issue", "comment"))
    assert "code-check 1.6.0" in comment.input and "hello" not in comment.input


@pytest.mark.parametrize("reply", ['["x"]', '[{"number": 3}]', '[{"number": "3 --x", "url": "u", "body": "b"}]', '{"a": 1}', "not json"])
def test_an_odd_gh_search_reply_falls_back_instead_of_raising(reply):
    r = FakeRunner().on(*LIST, stdout=reply)
    assert Filer(r, M).file("[code-check] boom", BODY).outcome == "fallback"


def test_gh_failure_falls_back_to_a_prefilled_url_and_never_raises():
    r = FakeRunner().on(*LIST, returncode=127, stderr="gh: not found")
    res = Filer(r, M).file("[code-check] boom & more", BODY)
    assert res.outcome == "fallback" and res.url.startswith("https://github.com/o/r/issues/new?title=")
    assert "%26" in res.url and "&more" not in res.url  # title is percent-encoded


def test_create_failure_also_falls_back():
    r = FakeRunner().on(*LIST, stdout="[]").on(*create_argv(), returncode=1, stderr="HTTP 401")
    r.on("gh", "issue", "create", "--repo", "o/r", "--title=[code-check] boom", "--body-file", "-", returncode=1, stderr="HTTP 401")
    assert Filer(r, M).file("[code-check] boom", BODY).outcome == "fallback"


@pytest.mark.parametrize("title", ["", "-x", "two\nlines", "x" * 200])
def test_bad_titles_are_refused_before_any_command(title):
    r = FakeRunner()
    with pytest.raises(CodeCheckError):
        Filer(r, M).file(title, BODY)
    assert r.calls == []


def test_body_without_fingerprint_skips_dedupe_but_still_files():
    r = FakeRunner().on(*create_argv(), stdout="https://github.com/o/r/issues/9")
    assert Filer(r, M).file("[code-check] boom", "no marker here").outcome == "created"


def test_text_the_user_added_is_scrubbed_before_it_leaves_the_machine():
    secret = "ghp_" + "a" * 30
    body = f"my words with {secret}\n<!-- feedback-fingerprint: abc123def456 -->"
    r = FakeRunner().on(*LIST, stdout="[]").on_prefix("gh", "issue", "create", stdout="https://github.com/o/r/issues/7")
    Filer(r, M).file(f"[code-check] boom {secret}", body)
    create = next(c for c in r.calls if c.argv[:3] == ("gh", "issue", "create"))
    assert secret not in create.input and not any(secret in a for a in create.argv)
    r = FakeRunner().on(*LIST, returncode=127)
    assert secret not in Filer(r, M).file(f"[code-check] boom {secret}", body).url  # nor in the prefilled URL


def test_an_oversized_edited_body_is_capped_and_keeps_its_fingerprint_marker():
    marker = "<!-- feedback-fingerprint: abc123def456 -->"
    r = FakeRunner().on(*LIST, stdout="[]").on(*create_argv(), stdout="https://github.com/o/r/issues/7")
    Filer(r, M).file("[code-check] boom", "x" * 50000 + "\n" + marker)
    create = next(c for c in r.calls if c.argv[:3] == ("gh", "issue", "create"))
    assert len(create.input) <= M.max_body_chars and create.input.endswith(marker)


def test_every_gh_call_has_a_timeout():
    class Recording(FakeRunner):
        def __init__(self):
            super().__init__()
            self.timeouts = []

        def run(self, args, *, input=None, cwd=None, timeout=None, env=None):
            self.timeouts.append(timeout)
            return super().run(args, input=input, cwd=cwd, timeout=timeout, env=env)

    r = Recording().on(*LIST, stdout=json.dumps([{"number": 3, "url": "https://github.com/o/r/issues/3", "body": "x\\n" + BODY.splitlines()[-1]}]))
    r.on("gh", "issue", "comment", "3", "--repo", "o/r", "--body-file", "-")
    Filer(r, M).file("[code-check] boom", BODY)
    assert r.timeouts and all(t and t > 0 for t in r.timeouts)


def test_the_fallback_url_stays_short_even_when_every_character_needs_encoding():
    r = FakeRunner().on(*LIST, returncode=127)
    url = Filer(r, M).file("[code-check] boom", "😀😀😀&#% " * 2000 + "\n<!-- feedback-fingerprint: abc123def456 -->").url
    assert len(url) <= 7000 and url.startswith("https://github.com/o/r/issues/new?title=")


def test_a_search_hit_without_our_exact_marker_is_not_a_duplicate():
    r = FakeRunner().on(*LIST, stdout=json.dumps([{"number": 3, "url": "https://github.com/o/r/issues/3", "body": "mentions abc123def456 in passing"}]))
    r.on(*create_argv(), stdout="https://github.com/o/r/issues/9")
    assert Filer(r, M).file("[code-check] boom", BODY).outcome == "created"


def test_the_last_marker_in_the_body_is_the_one_searched_for():
    body = "<!-- feedback-fingerprint: 000000000000 -->\nnote\n<!-- feedback-fingerprint: abc123def456 -->"
    r = FakeRunner().on(*LIST, stdout="[]").on(*create_argv(), stdout="https://github.com/o/r/issues/9")
    assert Filer(r, M).file("[code-check] boom", body).outcome == "created"  # the search above is for abc123def456, not the first marker
