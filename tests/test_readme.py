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


def test_precedence_and_repo_restrictions_match_the_code():
    from codecheck.config import PERMISSION_MODES, REPO_FORBIDDEN

    assert ("environment (`CODE_CHECK_<SECTION>__<KEY>`" in README
            and README.index("your `~/.config/code-check/config.toml`") < README.index("the repository's `.code-check.toml`, then the defaults"))
    for key in REPO_FORBIDDEN:
        name = ".".join(key)
        assert (f"`{name}`" in README) or (f"`[{name}]`" in README), name
    assert all(f"`{m}`" in README for m in PERMISSION_MODES)


def test_loop_retry_and_lock_text_match_the_code():
    from codecheck.core.state import MAX_LOOP_FAILURES

    assert f"up to {MAX_LOOP_FAILURES} attempts per fingerprint" in README
    assert "no automatic retry" not in README and "no locking of its own" not in README
    assert "run_in_background" in README and "state.json.lock" in README


def test_install_does_not_lead_with_an_unpublished_npm_name():
    install = README[README.index("## Install"):README.index("## Quick start")]
    first_npx = install.index("npx code-check")
    assert "not published to npm yet" in install[:first_npx]
    assert "node installer/cli.js install" in install and "npx github:subhiandrews/codecheck install" in install
    assert "plugin must be installed" in install
    assert "## Safety" in README and "### Prompt injection and unattended runs" in README


def test_documents_security_review_and_code_check_md():
    assert "## Security review" in README and "## CODE_CHECK.md" in README
    assert 'security = "opus"' in README and "models.security" in README
    assert "security = false" in README and "review.security" in README
    section = README.split("## CODE_CHECK.md", 1)[1]
    assert "16,000" in section and "base branch" in section and "symlink" in section
    review = README.split("## Security review", 1)[1].split("## CODE_CHECK.md", 1)[0]
    assert "--security" in review and "never to print a secret or PII value" in review and "not a replacement" in review


def test_repo_code_check_md_has_the_two_instructions():
    text = (Path(__file__).resolve().parent.parent / "CODE_CHECK.md").read_text()
    low = text.lower()
    assert "skills/*/SKILL.md" in text
    assert "duplication" in low
    assert "tests" in low
    assert "tautolog" in low
