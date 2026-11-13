import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_license_file_is_apache_2_0():
    text = (ROOT / "LICENSE").read_text()
    assert "Apache License" in text and "Version 2.0, January 2004" in text
    assert "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION" in text


def test_notice_names_the_copyright_holder_and_year():
    assert "Copyright 2026 Subhi Andrews" in (ROOT / "NOTICE").read_text()


def test_package_metadata_declares_apache_2_0():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["license"] == "Apache-2.0"
    assert project["license-files"] == ["LICENSE", "NOTICE"]
    pkg = json.loads((ROOT / "package.json").read_text())
    assert pkg["license"] == "Apache-2.0"
    assert {"LICENSE", "NOTICE"} <= set(pkg["files"])


def test_readme_points_to_license_and_contributing_files():
    readme = (ROOT / "README.md").read_text()
    assert "## License" in readme and "Apache License 2.0" in readme
    for name in ("CONTRIBUTING.md", "SECURITY.md"):
        assert (ROOT / name).is_file()
        assert f"({name})" in readme


def test_readme_install_commands_use_the_real_repository():
    readme = (ROOT / "README.md").read_text()
    assert "claude plugin marketplace add subhiandrews/codecheck" in readme
    assert "npx github:subhiandrews/codecheck install" in readme
    assert "marketplace add OWNER/REPO" not in readme
