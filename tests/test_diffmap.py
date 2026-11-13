import difflib

from hypothesis import given
from hypothesis import strategies as st

from codecheck.core.diffmap import Anchor, anchor_finding, parse_diff

MODIFIED = """diff --git a/src/app.py b/src/app.py
index 111..222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,4 +1,5 @@
 import os
-x = 1
+x = 2
+y = 3
 print(x)
 print(os)
@@ -20,3 +21,3 @@ def f():
 a
-b
+c
 d
"""


def test_modified_file_line_numbers_across_hunks():
    fd = parse_diff(MODIFIED)["src/app.py"]
    right = fd.right_lines()
    assert right[1] == "import os" and right[2] == "x = 2" and right[3] == "y = 3"
    assert right[21] == "a" and right[22] == "c" and right[23] == "d"
    assert fd.status == "modified" and not fd.binary


def test_added_deleted_renamed_binary():
    text = (
        "diff --git a/new.txt b/new.txt\nnew file mode 100644\n--- /dev/null\n+++ b/new.txt\n"
        "@@ -0,0 +1,2 @@\n+one\n+two\n"
        "diff --git a/gone.txt b/gone.txt\ndeleted file mode 100644\n--- a/gone.txt\n+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n-x\n-y\n"
        "diff --git a/old.py b/moved.py\nsimilarity index 100%\nrename from old.py\nrename to moved.py\n"
        "diff --git a/img.png b/img.png\nBinary files a/img.png and b/img.png differ\n"
    )
    files = parse_diff(text)
    assert files["new.txt"].status == "added" and files["new.txt"].right_lines() == {1: "one", 2: "two"}
    assert files["gone.txt"].status == "deleted" and files["gone.txt"].right_lines() == {}
    assert files["moved.py"].status == "renamed" and files["moved.py"].old_path == "old.py"
    assert files["img.png"].binary


def test_path_with_spaces_quotes_and_unicode():
    text = (
        'diff --git a/my file.txt b/my file.txt\n--- a/my file.txt\t\n+++ b/my file.txt\t\n'
        "@@ -1 +1 @@\n-a\n+b\n"
        'diff --git "a/caf\\303\\251.txt" "b/caf\\303\\251.txt"\n--- "a/caf\\303\\251.txt"\n+++ "b/caf\\303\\251.txt"\n'
        "@@ -1 +1 @@\n-a\n+b\n"
    )
    files = parse_diff(text)
    assert "my file.txt" in files
    assert "café.txt" in files


def test_deleted_line_that_looks_like_a_header_is_content():
    # The removed line is "-- note" which diff renders as "--- note".
    text = "diff --git a/f.sql b/f.sql\n--- a/f.sql\n+++ b/f.sql\n@@ -1,2 +1,2 @@\n--- note\n+-- changed\n keep\n"
    fd = parse_diff(text)["f.sql"]
    assert fd.right_lines() == {1: "-- changed", 2: "keep"}


def test_crlf_and_unicode_separators_do_not_shift_line_numbers():
    text = "diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n@@ -1,2 +1,3 @@\n a\r\n+b still line two\n c\x0b\n"
    right = parse_diff(text)["f.txt"].right_lines()
    assert right == {1: "a\r", 2: "b still line two", 3: "c\x0b"}


def test_no_newline_marker_is_ignored():
    text = "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b\n\\ No newline at end of file\n"
    assert parse_diff(text)["f"].right_lines() == {1: "b"}


def test_anchor_exact_relocated_and_dropped():
    files = parse_diff(MODIFIED)
    assert anchor_finding(files, "src/app.py", 2) == Anchor("src/app.py", 2)
    moved = anchor_finding(files, "src/app.py", 7)  # just below the first hunk (ends at line 5)
    assert moved is not None and moved.relocated and moved.line == 5
    assert anchor_finding(files, "src/app.py", 12) is None
    assert anchor_finding(files, "other.py", 1) is None


def test_deleted_and_binary_files_cannot_be_anchored():
    files = parse_diff(
        "diff --git a/gone b/gone\ndeleted file mode 100644\n--- a/gone\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
        "diff --git a/i.png b/i.png\nBinary files a/i.png and b/i.png differ\n"
    )
    assert anchor_finding(files, "gone", 1) is None
    assert anchor_finding(files, "i.png", 1) is None


line_text = st.text(alphabet=st.characters(blacklist_characters="\n", blacklist_categories=("Cs",)), max_size=12)


@given(old=st.lists(line_text, max_size=30), new=st.lists(line_text, max_size=30), probe=st.integers(1, 40))
def test_right_lines_match_new_file_and_anchors_are_commentable(old, new, probe):
    body = "\n".join(difflib.unified_diff(old, new, "a/f.txt", "b/f.txt", lineterm="", n=3))
    files = parse_diff("diff --git a/f.txt b/f.txt\n" + body + "\n")
    fd = files["f.txt"]
    for n, text in fd.right_lines().items():
        assert new[n - 1] == text
    anchor = anchor_finding(files, "f.txt", probe)
    if probe in fd.right_lines():
        assert anchor == Anchor("f.txt", probe)
    elif anchor is not None:
        assert anchor.line in fd.right_lines() and anchor.relocated
