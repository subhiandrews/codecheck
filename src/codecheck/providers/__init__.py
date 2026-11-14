from pathlib import Path

from codecheck.errors import ConfigError
from codecheck.providers.base import Provider
from codecheck.providers.github import GitHubProvider
from codecheck.runner import CommandRunner

PROVIDERS = {"github": GitHubProvider}


def get_provider(name: str, runner: CommandRunner, cwd: Path | None = None, login: str = "") -> Provider:
    try:
        return PROVIDERS[name](runner, cwd, login)
    except KeyError:
        raise ConfigError(f"unknown provider {name!r}; available: {', '.join(sorted(PROVIDERS))}") from None
