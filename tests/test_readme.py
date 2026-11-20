import re
from dataclasses import fields, is_dataclass
from pathlib import Path

from codecheck import commands
from codecheck.config import Config

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text()
DOCS = ROOT / "docs"
ARCH = (DOCS / "ARCHITECTURE.md").read_text()
CONF = (DOCS / "CONFIGURATION.md").read_text()
MAINT = (DOCS / "MAINTAINING.md").read_text()

# About five or six printed pages. CODE_CHECK.md tells reviewers the same number.
README_MAX_LINES = 260


def test_readme_stays_within_a_few_pages():
    assert len(README.splitlines()) <= README_MAX_LINES


def test_readme_links_resolve_to_files():
    for target in re.findall(r"\]\(((?:docs/|\.\./)?[A-Za-z0-9_./-]+\.md)(?:#[^)]*)?\)", README):
        assert (ROOT / target).is_file(), target
    for doc in (ARCH, CONF, MAINT):
        for target in re.findall(r"\]\(([A-Za-z0-9_./-]+\.md)(?:#[^)]*)?\)", doc):
            assert (DOCS / target).resolve().is_file(), target


def test_every_mentioned_subcommand_exists():
    text = README.replace("npx code-check", "NPX").replace("pip install code-check", "PIP")
    used = set(re.findall(r"(?<![\w-])code-check (?:--cwd \S+ )?([a-z][a-z-]*)", text))
    known = {m.NAME for m in commands.ALL}
    assert used <= known | {"is", "reviews", "means", "and", "plugin", "skills", "to", "in", "the", "a"}, used - known


def test_names_every_subcommand_skill_and_agent():
    public = {m.NAME for m in commands.ALL} - {"handoff", "handoff-queue"}
    def named(name):  # in backticks (the tables) or as `code-check <name>`, never as a word in prose
        return re.search(rf"`(?:code-check )?{re.escape(name)}[ `]", README) is not None

    assert not [n for n in sorted(public) if not named(n)], "subcommands missing from README"
    skills = sorted(p.name for p in (ROOT / "skills").iterdir() if p.is_dir())
    assert not [s for s in skills if not named(s)], "skills missing from README"
    agents = sorted(p.stem for p in (ROOT / "agents").glob("*.md"))
    langs = [a for a in agents if a.startswith("lang-")]
    others = [a for a in agents if not a.startswith("lang-")]
    assert not [a for a in others if not named(a)], "agents missing from README"
    assert not [a[5:] for a in langs if re.search(rf"\b{re.escape(a[5:])}\b", README) is None], "languages missing"


def test_states_host_and_runtime_support_and_install():
    for word in ("GitHub", "GitLab", "Claude Code", "Codex", "gh auth login", "glab auth login",
                 "--target codex", "--provider github|gitlab|both", "3.14.10"):
        assert word in README, word
    assert "## Install" in README and "## Skills" in README and "## Safety" in README
    for doc in ("ARCHITECTURE.md", "CONFIGURATION.md", "MAINTAINING.md", "INSTALL-CODEX.md"):
        assert f"docs/{doc}" in README, doc


def test_install_does_not_lead_with_an_unpublished_npm_name():
    install = README[README.index("## Install"):README.index("## Quick start")]
    assert "not on npm yet" in install
    assert install.index("not on npm yet") < install.index("`npx code-check`")
    assert "npx github:subhiandrews/codecheck install" in install and "node installer/cli.js install" in install


def test_documents_every_config_key():
    def keys(cls, prefix=""):
        for f in fields(cls):
            value = getattr(cls(), f.name)
            if is_dataclass(value):
                yield from keys(type(value), f"{f.name}.")
            else:
                yield f"{prefix}{f.name}"

    text = README + CONF
    missing = [k for k in keys(Config) if k.split(".")[-1] not in text]
    assert not missing, f"undocumented config keys: {missing}"


def test_precedence_and_repo_restrictions_match_the_code():
    from codecheck.config import PERMISSION_MODES, REPO_FORBIDDEN

    assert "environment (`CODE_CHECK_<SECTION>__<KEY>`" in README
    assert README.index("`~/.config/code-check/config.toml`") < README.index("the repository's `.code-check.toml`, defaults")
    for key in REPO_FORBIDDEN:
        name = ".".join(key)
        assert any(f in t for t in (README, CONF) for f in (f"`{name}`", f"`[{name}]`")), name
    assert all(f"`{m}`" in CONF for m in PERMISSION_MODES)


def test_loop_retry_and_lock_text_match_the_code():
    from codecheck.core.state import MAX_LOOP_FAILURES

    assert f"up to {MAX_LOOP_FAILURES} attempts per fingerprint" in README
    assert f"up to {MAX_LOOP_FAILURES} attempts per fingerprint" in ARCH
    assert "no automatic retry" not in ARCH and "no locking of its own" not in ARCH
    assert "run_in_background" in ARCH and "state.json.lock" in ARCH


def test_architecture_keeps_the_design_sections():
    for heading in ("## Review flow", "## GitLab differences", "## Security review", "## Performance review",
                    "## CODE_CHECK.md", "## Loops", "## Safety model", "### Prompt injection and unattended runs"):
        assert heading in ARCH, heading
    assert "```mermaid" in ARCH


def test_maintainer_doc_keeps_the_tooling_sections():
    for heading in ("## Shadow mode", "### Hand-off data", "### Golden cases and replay", "### Live snippet eval",
                    "## Hand-off rounds (maintainers)", "## Development", "## Extending"):
        assert heading in MAINT, heading


def test_documents_security_review_and_code_check_md():
    assert "## CODE_CHECK.md" in ARCH and "### CODE_CHECK.md" in README
    assert 'security = "opus"' in CONF and "models.security" in ARCH
    assert "security = false" in CONF and "review.security" in ARCH
    section = ARCH.split("## CODE_CHECK.md", 1)[1]
    assert "16,000" in section and "base branch" in section and "symlink" in section
    assert "16,000" in README and "base branch" in README
    review = ARCH.split("## Security review", 1)[1].split("## Performance review", 1)[0]
    assert "--security" in review and "never to print a secret or PII value" in review and "not a replacement" in review


def test_documents_performance_review():
    assert 'performance = "sonnet"' in CONF and "models.performance" in ARCH
    assert "performance = false" in CONF and "review.performance" in ARCH
    section = ARCH.split("## Performance review", 1)[1].split("## CODE_CHECK.md", 1)[0]
    assert "--performance" in section and "complexity before and after" in section and "not a replacement" in section


def test_documents_shadow_and_codex_settings():
    assert "## Shadow and golden settings" in CONF
    for key in ("shadow.handoff", "golden.licenses", "codex.max_parallel", "[codex_models]"):
        assert key in CONF, key


def test_repo_code_check_md_has_the_two_instructions():
    text = (ROOT / "CODE_CHECK.md").read_text()
    low = text.lower()
    assert "skills/*/SKILL.md" in text
    assert "duplication" in low
    assert "tests" in low
    assert "tautolog" in low


def test_repo_code_check_md_guards_the_docs():
    text = (ROOT / "CODE_CHECK.md").read_text()
    assert "README.md" in text and "docs/ARCHITECTURE.md" in text
    assert str(README_MAX_LINES) in text
    for subject in ("skills", "agents", "subcommand", "config key", "GitLab", "Codex"):
        assert subject in text, subject
