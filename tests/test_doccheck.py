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
    for p in ("README.md", "docs/a.rst", "CHANGELOG", "guide.rst", "a/b/NOTES.adoc", "docs/decisions/0001-x.md"):
        assert is_doc_path(p), p
    for p in ("src/a.py", "tests/readme_test.py", "data.json", "requirements.txt", "skills/x/SKILL.md", "agents/docs.md",
              ".claude/agents/a.md", "pkg/agents/b.md"):
        assert not is_doc_path(p), p


def test_doc_files_report_their_added_lines():
    diffs = _diff("docs/new.md", "+# T\n+body\n")
    m = measure(diffs, {"docs/new.md": None}, 3)
    assert m.doc_files == ({"path": "docs/new.md", "status": "added", "added_lines": 2},)


def test_a_run_that_starts_inside_a_docstring_closes_it():
    lines = ['"""', "x = 1", "y = 2"]
    assert comment_flags("python", lines, mid=True) == [True, False, False]
    assert comment_flags("python", lines) == [True, True, True]  # a file that opens with it opens a docstring
    assert comment_flags("c", ["*/", "int x;"], mid=True) == [True, False]


def test_python_string_assigned_in_code_is_code_not_a_docstring():
    lines = ['QUERY = """', "SELECT 1", '"""', "def f():", "    return run(QUERY)"]
    assert comment_flags("python", lines) == [False, False, False, False, False]
    assert comment_flags("python", ['x = dedent("""', "a", '""")', "y = 1"]) == [False, False, False, False]


def test_a_run_below_a_multiline_docstring_closes_it():
    lines = ["    More text.", '    """', "    x = 1", "    y = 2"]
    assert comment_flags("python", lines, mid=True) == [True, True, False, False]
    assert comment_flags("python", ['"""one"""', "x = 1"], mid=True) == [True, False]  # balanced: nothing is open


def test_over_cap_needs_the_minimum_number_of_comment_lines():
    body = "".join(f"+{ln}\n" for ln in ["# why", "x = 1"] + ["y = 2"] * 8)
    diffs = _diff("a.py", body)
    assert measure(diffs, {"a.py": "python"}, 3).over_cap
    m = measure(diffs, {"a.py": "python"}, 3, 3)
    assert not m.over_cap and m.to_json()["over_cap"] is False
