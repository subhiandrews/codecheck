from pathlib import Path

from codecheck.errors import ConfigError
from codecheck.providers.base import Provider
from codecheck.providers.github import GitHubProvider
from codecheck.providers.gitlab import GitLabProvider, remote_host
from codecheck.runner import CommandRunner

PROVIDERS = {"github": GitHubProvider, "gitlab": GitLabProvider}


def detect_provider(name: str, remote_url: str) -> str:
    """Resolve `auto` from the remote: gitlab.com means GitLab, anything else (or no remote) means GitHub.

    An explicit provider name is returned as it is, so `provider = "github"` is never overridden."""
    if name != "auto":
        return name
    return "gitlab" if remote_host(remote_url) == "gitlab.com" else "github"


def get_provider(name: str, runner: CommandRunner, cwd: Path | None = None, login: str = "") -> Provider:
    try:
        return PROVIDERS[name](runner, cwd, login)
    except KeyError:
        raise ConfigError(f"unknown provider {name!r}; available: {', '.join(sorted(PROVIDERS))}") from None
