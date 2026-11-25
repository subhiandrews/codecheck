"""What a host plugin tells the feedback code about itself: where issues go, and what kinds of signal exist."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from codecheck.errors import ConfigError

REPO = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}")
KIND = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


@dataclass(frozen=True)
class Manifest:
    product: str
    version: str
    target_repo: str
    kinds: Mapping[str, str] = field(default_factory=dict)
    max_body_chars: int = 6000

    def __post_init__(self) -> None:
        if not REPO.fullmatch(self.target_repo):
            raise ConfigError(f"feedback target_repo must look like OWNER/REPO: {self.target_repo!r}")
        for kind in self.kinds:
            if not KIND.fullmatch(kind):
                raise ConfigError(f"feedback kind is not valid: {kind!r}")

    def title_for(self, kind: str) -> str:
        try:
            return self.kinds[kind]
        except KeyError:
            raise ConfigError(f"unknown feedback kind {kind!r}; known: {', '.join(sorted(self.kinds))}") from None

