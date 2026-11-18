from pathlib import Path

import codecheck


def test_version_is_exposed():
    # The release invariant: the package version is the VERSION file's, so a bump needs no test edit.
    assert codecheck.__version__ == (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
