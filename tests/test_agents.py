from pathlib import Path

from codecheck.agentgen import LANGUAGE_FOCUS, render_all
from codecheck.config import ModelsConfig
from codecheck.core.panel import LANGUAGES

REPO = Path(__file__).resolve().parent.parent


def frontmatter(text: str) -> dict:
    head = text.split("---\n")[1]
    return dict(line.split(": ", 1) for line in head.strip().splitlines())


def test_one_expert_per_language_and_all_files_present():
    assert set(LANGUAGE_FOCUS) == set(LANGUAGES)
    files = render_all(ModelsConfig())
    assert len(files) == 21
    assert {f"lang-{lang}.md" for lang in LANGUAGES} <= set(files)
    assert {"architect.md", "pragmatist.md", "skeptic.md", "aggregator.md", "deep-reviewer.md"} <= set(files)


def test_default_models_and_tools():
    files = render_all(ModelsConfig())
    assert frontmatter(files["aggregator.md"])["model"] == "opus"
    assert frontmatter(files["architect.md"])["model"] == "sonnet"
    assert frontmatter(files["lang-python.md"])["model"] == "haiku"
    for name, text in files.items():
        fm = frontmatter(text)
        assert fm["name"] == name.removesuffix(".md") and fm["description"]
        assert fm["tools"] == "Read, Grep, Glob, Write"


def test_models_follow_config_including_per_language():
    files = render_all(ModelsConfig(language_experts="sonnet", reviewers="opus", per_language={"rust": "opus"}))
    assert frontmatter(files["lang-rust.md"])["model"] == "opus"
    assert frontmatter(files["lang-go.md"])["model"] == "sonnet"
    assert frontmatter(files["skeptic.md"])["model"] == "opus"


def test_prompts_carry_the_protocol():
    files = render_all(ModelsConfig())
    for name in ("architect.md", "lang-go.md", "skeptic.md", "pragmatist.md"):
        text = files[name]
        assert "findings/" in text and "context.json" in text and "NEW-file line numbers" in text
        assert "critical" in text and "suggestion" in text
    assert "ingest.json" in files["aggregator.md"] and "dropped" in files["aggregator.md"]
    assert "write `[]`" in files["deep-reviewer.md"]


def test_committed_agents_match_generator_output():
    expected = render_all(ModelsConfig())
    committed = {p.name: p.read_text() for p in (REPO / "agents").glob("*.md")}
    assert committed == expected, "agents/ is stale: run `code-check gen-agents --out agents`"


def test_security_agent_uses_the_security_model_and_has_its_own_focus():
    files = render_all(ModelsConfig())
    assert frontmatter(files["security.md"])["model"] == "opus"
    assert frontmatter(render_all(ModelsConfig(security="sonnet"))["security.md"])["model"] == "sonnet"
    for name in ("architect.md", "pragmatist.md", "skeptic.md"):
        assert frontmatter(files[name])["model"] == "sonnet"
    assert "senior security reviewer" in files["security.md"] and "category `security`" in files["security.md"]


def test_security_agent_forbids_echoing_secrets():
    body = render_all(ModelsConfig())["security.md"].lower()
    assert "you must never print a secret or pii value in `message` or `suggestion`" in body
    assert "never quote, echo or partially reproduce the value itself" in body
