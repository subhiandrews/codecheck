import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_top_changelog_entry_is_the_current_version():
    headings = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", (ROOT / "CHANGELOG.md").read_text(), re.MULTILINE)
    assert headings, "CHANGELOG.md has no `## [x.y.z]` entry"
    assert headings[0] == (ROOT / "VERSION").read_text().strip()
