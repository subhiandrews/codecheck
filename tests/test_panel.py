from codecheck.core.panel import (
    LANGUAGES,
    detect_language,
    filter_ignored,
    group_by_language,
    is_ignored,
    select_panel,
)


def test_fifteen_languages():
    assert len(LANGUAGES) == 15


def test_detect_by_extension_and_shebang():
    assert detect_language("src/a.PY") == "python"
    assert detect_language("web/app.tsx") == "typescript"
    assert detect_language("lib/x.hpp") == "cpp"
    assert detect_language("tool", "#!/usr/bin/env python3") == "python"
    assert detect_language("run", "#!/bin/bash") == "shell"
    assert detect_language("notes.txt") is None
    assert detect_language("Makefile") is None


def test_group_and_panel_only_includes_present_languages():
    paths = ["a.py", "b.py", "c.go", "README.md"]
    assert group_by_language(paths) == {"python": ["a.py", "b.py"], "go": ["c.go"]}
    panel = select_panel(paths)
    assert panel.core == ("architect", "pragmatist", "skeptic")
    assert panel.languages == ("go", "python")
    assert panel.members == ("architect", "pragmatist", "skeptic", "lang-go", "lang-python")


def test_deep_reviewer_is_optional_and_never_disruptive():
    assert "deep-reviewer" not in select_panel(["a.py"]).members
    assert "deep-reviewer" not in select_panel(["a.py"], deep_enabled=True, deep_available=False).members
    unavailable = select_panel(["a.py"], deep_enabled=True, deep_available=False)
    assert any("deep" in n for n in unavailable.notes)
    assert "deep-reviewer" in select_panel(["a.py"], deep_enabled=True, deep_available=True).members


def test_ignore_globs():
    pats = ("**/*.lock", "dist/**")
    assert is_ignored("poetry.lock", pats) and is_ignored("a/b/yarn.lock", pats)
    assert is_ignored("dist/app.js", pats)
    assert not is_ignored("src/dist.py", pats)
    assert filter_ignored(["a.py", "x.lock"], pats) == ["a.py"]
