import re
from dataclasses import fields, is_dataclass
from pathlib import Path

from codecheck import commands
from codecheck.config import Config

README = (Path(__file__).resolve().parent.parent / "README.md").read_text()


def test_every_mentioned_subcommand_exists():
    text = README.replace("npx code-check", "NPX").replace("pip install code-check", "PIP")
    used = set(re.findall(r"(?<![\w-])code-check (?:--cwd \S+ )?([a-z][a-z-]*)", text))
    known = {m.NAME for m in commands.ALL}
    assert used <= known | {"is", "reviews", "means", "and", "plugin", "skills", "to", "in", "the", "a"}, used - known


def test_documents_every_config_key():
    def keys(cls, prefix=""):
        for f in fields(cls):
            value = getattr(cls(), f.name)
            if is_dataclass(value):
                yield from keys(type(value), f"{f.name}.")
            else:
                yield f"{prefix}{f.name}"

    missing = [k for k in keys(Config) if k.split(".")[-1] not in README]
    assert not missing, f"undocumented config keys: {missing}"


def test_has_the_sections_the_spec_promises():
    for heading in ("## Install", "## Quick start", "## Configuration", "## Loops", "## Safety",
                    "## Shadow mode", "## Extending", "## Troubleshooting"):
        assert heading in README
    for skill in ("review-local", "review-pr", "address-comments", "babysit-pr"):
        assert skill in README
    assert "3.14.10" in README
