import pytest
from fakes import FakeRunner

from codecheck.errors import ConfigError
from codecheck.providers import get_provider
from codecheck.providers.github import GitHubProvider


def test_registry_returns_github_and_rejects_unknown():
    assert isinstance(get_provider("github", FakeRunner()), GitHubProvider)
    with pytest.raises(ConfigError, match="github"):
        get_provider("gitlab", FakeRunner())
