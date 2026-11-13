from codecheck.core.precheck import precheck_local, precheck_pr
from codecheck.core.state import StateStore
from codecheck.models import PRRef, PullRequest

REF = PRRef("o/r", 7)


def pr(**over):
    base = {"ref": REF, "title": "t", "body": "", "author": "alice", "base_ref": "main", "head_ref": "feat",
                "head_sha": "abc123", "is_draft": False, "state": "open", "url": "u"}
    return PullRequest(**{**base, **over})


def test_pr_precheck_reasons(tmp_path):
    s = StateStore(tmp_path / "s.json")
    kw = {"ignore": ("**/*.lock",), "me": "me"}
    assert not precheck_pr(pr(state="closed"), ["a.py"], s, **kw).proceed
    assert "draft" in precheck_pr(pr(is_draft=True), ["a.py"], s, **kw).reason
    assert "own PR" in precheck_pr(pr(author="me"), ["a.py"], s, **kw).reason
    assert "no reviewable" in precheck_pr(pr(), ["x.lock"], s, **kw).reason
    ok = precheck_pr(pr(), ["a.py", "x.lock"], s, **kw)
    assert ok.proceed and ok.files == ("a.py",) and ok.fingerprint == "abc123"


def test_pr_precheck_skips_already_reviewed_head_unless_forced(tmp_path):
    s = StateStore(tmp_path / "s.json")
    s.set(f"review:{REF}", "abc123")
    kw = {"ignore": (), "me": "me"}
    assert "already reviewed" in precheck_pr(pr(), ["a.py"], s, **kw).reason
    assert precheck_pr(pr(), ["a.py"], s, force=True, **kw).proceed
    assert precheck_pr(pr(head_sha="def456"), ["a.py"], s, **kw).proceed


def test_local_precheck(tmp_path):
    s = StateStore(tmp_path / "s.json")
    assert "no changes" in precheck_local([], "fp", "local:k", s, ignore=()).reason
    assert "no reviewable" in precheck_local(["x.lock"], "fp", "local:k", s, ignore=("**/*.lock",)).reason
    assert precheck_local(["a.py"], "fp", "local:k", s, ignore=()).proceed
    s.set("local:k", "fp")
    assert "unchanged" in precheck_local(["a.py"], "fp", "local:k", s, ignore=()).reason
    assert precheck_local(["a.py"], "fp2", "local:k", s, ignore=()).proceed
    assert precheck_local(["a.py"], "fp", "local:k", s, ignore=(), force=True).proceed
