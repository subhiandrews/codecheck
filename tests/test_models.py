import pytest

from codecheck.models import Finding, PRRef, Severity


def test_prref_roundtrip():
    ref = PRRef.parse("octo/repo#42")
    assert ref == PRRef("octo/repo", 42)
    assert str(ref) == "octo/repo#42"


@pytest.mark.parametrize("bad", ["octo/repo", "repo#4", "a/b/c#1", "o/r#x", ""])
def test_prref_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        PRRef.parse(bad)


def test_severity_ordering_and_label():
    assert Severity.CRITICAL.rank > Severity.WARNING.rank > Severity.SUGGESTION.rank
    assert Severity.WARNING.label == "Warning"


def test_finding_to_dict_is_json_safe():
    f = Finding("a.py", 3, Severity.WARNING, "bug", "msg", reviewers=("skeptic",))
    assert f.to_dict()["severity"] == "warning"
    assert f.to_dict()["reviewers"] == ["skeptic"]
