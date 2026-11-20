from codecheck.core.diffmap import parse_diff
from codecheck.core.doccheck import comment_flags, is_doc_path, measure


def test_hash_languages_and_shebang():
    assert comment_flags("shell", ["#!/bin/sh", "# note", "echo hi", ""]) == [False, True, False, None]
    assert comment_flags("ruby", ["# note", "x = 1  # trailing"]) == [True, False]


def test_python_docstrings_span_lines():
    lines = ['def f():', '    """Summary.', '', '    More.', '    """', '    return 1', '    """one line"""']
    assert comment_flags("python", lines) == [False, True, None, True, True, False, True]


def test_block_comments_span_lines():
    lines = ["/* start", " * middle", " */", "int x;", "/* one */", "// line", "int y; // trailing"]
    assert comment_flags("c", lines) == [True, True, True, False, True, True, False]


def test_sql_uses_dashes():
    assert comment_flags("sql", ["-- why", "SELECT 1;", "/* x */"]) == [True, False, True]


def _diff(path, body, header="new file mode 100644\n"):
    n = body.count("\n")
    return parse_diff(f"diff --git a/{path} b/{path}\n{header}--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{n} @@\n{body}")


def test_measure_counts_only_added_lines_per_language():
    body = "".join(f"+{ln}\n" for ln in ["# a", "x = 1", "y = 2", "z = 3", ""])
    diffs = _diff("a.py", body)
    m = measure(diffs, {"a.py": "python"}, 3)
    assert (m.comment_lines, m.code_lines) == (1, 3)
    assert m.percent == 25.0 and m.over_cap


def test_measure_ignores_files_without_a_language_and_reports_under_cap():
    code = "".join(f"+x{i} = {i}\n" for i in range(40)) + "+# why\n"
    diffs = _diff("a.py", code) | _diff("notes.txt", "+hello\n")
    m = measure(diffs, {"a.py": "python", "notes.txt": None}, 3)
    assert (m.comment_lines, m.code_lines) == (1, 40) and not m.over_cap


def test_context_lines_keep_a_docstring_open_but_are_not_counted():
    text = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,3 +1,4 @@\n"
        ' """Doc\n+more doc\n """\n x = 1\n'
    )
    m = measure(parse_diff(text), {"a.py": "python"}, 3)
    assert (m.comment_lines, m.code_lines) == (1, 0)


def test_doc_paths():
    for p in ("README.md", "docs/a.txt", "CHANGELOG", "guide.rst", "a/b/NOTES.adoc", "docs/decisions/0001-x.md"):
        assert is_doc_path(p), p
    for p in ("src/a.py", "tests/readme_test.py", "data.json"):
        assert not is_doc_path(p), p


def test_doc_files_report_their_added_lines():
    diffs = _diff("docs/new.md", "+# T\n+body\n")
    m = measure(diffs, {"docs/new.md": None}, 3)
    assert m.doc_files == ({"path": "docs/new.md", "status": "added", "added_lines": 2},)
