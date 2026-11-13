"""Layered configuration: defaults < user file < repo file < environment."""

import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path

from codecheck.errors import ConfigError


@dataclass(frozen=True, slots=True)
class ReviewConfig:
    auto_approve: bool = False
    max_files: int = 80
    max_chunk_chars: int = 120_000
    max_inline_comments: int = 25
    ignore: tuple[str, ...] = ("**/*.lock", "dist/**")


@dataclass(frozen=True, slots=True)
class LoopConfig:
    max_prs_per_iteration: int = 5
    repos: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelsConfig:
    aggregator: str = "opus"
    language_experts: str = "haiku"
    reviewers: str = "sonnet"
    deep: str = "sonnet"
    per_language: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DeepReviewerConfig:
    enabled: bool = False
    command: str = ""


@dataclass(frozen=True, slots=True)
class ShadowConfig:
    enabled: bool = True
    store: str = "~/.local/share/code-check/shadow"


@dataclass(frozen=True, slots=True)
class AddressConfig:
    test_command: str = ""
    resolve_fixed_threads: bool = True
    max_ci_attempts: int = 3


@dataclass(frozen=True, slots=True)
class RunPrConfig:
    claude_bin: str = "claude"
    timeout_seconds: int = 1800
    permission_mode: str = "dontAsk"
    allowed_tools: tuple[str, ...] = ("Read", "Grep", "Glob", "Write", "Edit", "Agent", "Bash")
    review_prompt: str = (
        "Use the code-check review-pr skill on {ref}. Run unattended: never ask questions; "
        "finish with one line summarizing the outcome."
    )
    babysit_prompt: str = (
        "Use the code-check babysit-pr skill on {ref}. Run unattended: never ask questions; "
        "finish with one line summarizing the outcome."
    )


@dataclass(frozen=True, slots=True)
class Config:
    provider: str = "github"
    base_branch: str = "auto"
    remote: str = "origin"
    state_dir: str = "~/.local/share/code-check/state"
    review: ReviewConfig = field(default_factory=ReviewConfig)
    loop: LoopConfig = field(default_factory=LoopConfig)
    models: ModelsConfig = field(default_factory=ModelsConfig)
    deep_reviewer: DeepReviewerConfig = field(default_factory=DeepReviewerConfig)
    shadow: ShadowConfig = field(default_factory=ShadowConfig)
    address: AddressConfig = field(default_factory=AddressConfig)
    run_pr: RunPrConfig = field(default_factory=RunPrConfig)


_FREEFORM = {("models", "per_language")}
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def expand_path(value: str) -> Path:
    return Path(value).expanduser()


def model_for(models: ModelsConfig, role: str, language: str | None = None) -> str:
    if role == "language_expert":
        if language and language in models.per_language:
            return models.per_language[language]
        return models.language_experts
    if role in ("aggregator", "reviewers", "deep"):
        return getattr(models, role)
    raise ConfigError(f"unknown model role {role!r}")


def _type_ok(default: object, value: object) -> bool:
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, tuple):
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    return isinstance(value, type(default))


def _merge(base: dict, over: Mapping, path: tuple[str, ...] = ()) -> None:
    for key, value in over.items():
        here = (*path, key)
        if path in _FREEFORM:
            base[key] = value
            continue
        if key not in base:
            raise ConfigError(f"unknown config key: {'.'.join(here)}")
        if isinstance(base[key], dict):
            if not isinstance(value, Mapping):
                raise ConfigError(f"config key {'.'.join(here)} must be a table")
            _merge(base[key], value, here)
        elif not _type_ok(base[key], value):
            raise ConfigError(f"config key {'.'.join(here)} has the wrong type")
        else:
            base[key] = value


def _coerce(default: object, raw: str) -> object:
    if isinstance(default, bool):
        low = raw.strip().lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ConfigError(f"cannot read {raw!r} as a boolean")
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError as e:
            raise ConfigError(f"cannot read {raw!r} as an integer") from e
    if isinstance(default, tuple):
        return [p.strip() for p in raw.split(",") if p.strip()]
    return raw


def _apply_env(data: dict, env: Mapping[str, str]) -> None:
    for name, raw in env.items():
        if not name.startswith("CODE_CHECK_"):
            continue
        parts = name.removeprefix("CODE_CHECK_").lower().split("__")
        node = data
        for part in parts[:-1]:
            node = node.get(part) if isinstance(node, dict) else None
            if not isinstance(node, dict):
                break
        else:
            leaf = parts[-1]
            if leaf in node and not isinstance(node[leaf], dict):
                current = node[leaf]
                node[leaf] = _coerce(tuple(current) if isinstance(current, list) else current, raw)


def _build(cls, data: Mapping):
    default = cls()
    kwargs = {}
    for f in fields(cls):
        dv = getattr(default, f.name)
        value = data[f.name]
        if is_dataclass(dv):
            kwargs[f.name] = _build(type(dv), value)
        elif isinstance(dv, tuple):
            kwargs[f.name] = tuple(value)
        else:
            kwargs[f.name] = value
    return cls(**kwargs)


def _read(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (tomllib.TOMLDecodeError, OSError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e


def load_config(repo_dir: Path, home: Path, env: Mapping[str, str]) -> Config:
    data = asdict(Config())
    _merge(data, _read(home / ".config" / "code-check" / "config.toml"))
    _merge(data, _read(repo_dir / ".code-check.toml"))
    _apply_env(data, env)
    return _build(Config, data)
