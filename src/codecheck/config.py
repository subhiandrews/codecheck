"""Layered configuration: defaults < repo `.code-check.toml` < user file < environment (spec section 9).

Security-sensitive keys (REPO_FORBIDDEN) cannot come from the repository file: a repository is not
trusted to choose the tools an unattended run gets, where state lives, or whether reviews auto-approve.
"""

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path

from codecheck.errors import ConfigError


@dataclass(frozen=True, slots=True)
class RoutingConfig:
    """Which optional reviewers a change earns. `off` restores the fixed panel; `full` ignores all of it."""
    mode: str = "auto"  # "auto" or "off"
    small_max_lines: int = 40  # at most this many changed lines, in at most small_max_files files, skips the architect
    small_max_files: int = 3
    performance_min_score: int = 3  # score of the performance signals in the added lines that adds the reviewer


@dataclass(frozen=True, slots=True)
class ReviewConfig:
    auto_approve: bool = False
    security: bool = False
    performance: bool = False
    full: bool = False  # true: every reviewer on every review, routing skipped
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    attribution: bool = True  # one credit line in the review summary; user-only
    label: str = "code-check"  # label added to reviewed PRs, "" disables; user-only
    learn_guidance: bool = False  # review-local may write a checked CODE_CHECK.md proposal to the working tree; user-only
    login: str = ""  # act as this login instead of asking `gh api user` (CI tokens cannot); user-only
    ci_login: str = ""  # the login a CI review posts as; address-comments learns from its findings; user-only
    seed_comments: str = ""  # absolute path of a file that local `context` copies in as existing-comments.md (evals replay a later round); user-only
    max_files: int = 80
    max_chunk_chars: int = 120_000
    max_inline_comments: int = 25
    quiet_after_round: int = 3  # from this review round on, a PR gets suggestions only on lines changed since our last review; 0 disables
    ignore: tuple[str, ...] = ("**/*.lock", "dist/**")


@dataclass(frozen=True, slots=True)
class LoopConfig:
    max_prs_per_iteration: int = 5
    max_review_prs_per_iteration: int = 3  # review mode only: the newest open PRs of the repository come first
    repos: tuple[str, ...] = ()
    review_forks: bool = False  # the unattended review loop skips fork-based PRs unless this is true


@dataclass(frozen=True, slots=True)
class ModelsConfig:
    aggregator: str = "opus"
    language_experts: str = "haiku"
    reviewers: str = "sonnet"
    deep: str = "sonnet"
    security: str = "opus"
    performance: str = "sonnet"
    docs: str = "sonnet"
    judge: str = "opus"
    per_language: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CodexModelsConfig(ModelsConfig):
    aggregator: str = "gpt-6.2-astra"
    language_experts: str = "gpt-6-luna"
    reviewers: str = "gpt-6.2-sol"
    deep: str = "gpt-6.2-sol"
    security: str = "gpt-6.2-astra"
    performance: str = "gpt-6.2-sol"
    docs: str = "gpt-6.2-sol"
    judge: str = "gpt-6.2-astra"


@dataclass(frozen=True, slots=True)
class CodexReasoningConfig(ModelsConfig):
    aggregator: str = "high"
    language_experts: str = "high"
    reviewers: str = "medium"
    deep: str = "medium"
    security: str = "high"
    performance: str = "medium"
    docs: str = "medium"
    judge: str = "high"


@dataclass(frozen=True, slots=True)
class CodexConfig:
    max_parallel: int = 4
    timeout_seconds: int = 300
    allow_unmeasured_platform: bool = False


@dataclass(frozen=True, slots=True)
class DeepReviewerConfig:
    enabled: bool = False
    command: str = ""


@dataclass(frozen=True, slots=True)
class ShadowConfig:
    enabled: bool = True
    store: str = "~/.local/share/code-check/shadow"
    handoff: bool = False


# Licenses under which a reviewed PR's diff may be committed to the public golden set (lowercase SPDX ids).
DEFAULT_LICENSES = ("mit", "apache-2.0", "bsd-2-clause", "bsd-3-clause", "isc", "0bsd", "unlicense")


@dataclass(frozen=True, slots=True)
class GoldenConfig:
    licenses: tuple[str, ...] = DEFAULT_LICENSES  # `shadow promote --finalize` refuses any other license

    def __post_init__(self):
        object.__setattr__(self, "licenses", tuple(str(x).strip().lower() for x in self.licenses))


@dataclass(frozen=True, slots=True)
class AddressConfig:
    test_command: str = ""
    resolve_fixed_threads: bool = True
    max_ci_attempts: int = 3


@dataclass(frozen=True, slots=True)
class FeedbackConfig:
    enabled: bool = True  # false: nothing is recorded and nothing is ever asked
    min_interval_hours: int = 24  # at most one automatic ask per this many hours
    target_repo: str = ""  # "" = the manifest's repository (OWNER/REPO)


@dataclass(frozen=True, slots=True)
class RunPrConfig:
    claude_bin: str = "claude"
    timeout_seconds: int = 1800
    permission_mode: str = "dontAsk"
    # Passes `--add-dir <state_dir>` so the run may read and write the worktrees and run directories that
    # live under state_dir (outside its working directory).
    add_state_dir: bool = True
    # A review only reads code and writes findings files: no Edit.
    review_allowed_tools: tuple[str, ...] = ("Read", "Grep", "Glob", "Write", "Agent", "Bash")
    # Babysitting fixes code, so it also needs Edit.
    babysit_allowed_tools: tuple[str, ...] = ("Read", "Grep", "Glob", "Write", "Edit", "Agent", "Bash")
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
    provider: str = "auto"  # auto: GitLab for a gitlab.com remote, otherwise GitHub
    base_branch: str = "auto"
    remote: str = "origin"
    state_dir: str = "~/.local/share/code-check/state"
    review: ReviewConfig = field(default_factory=ReviewConfig)
    loop: LoopConfig = field(default_factory=LoopConfig)
    models: ModelsConfig = field(default_factory=ModelsConfig)
    codex_models: CodexModelsConfig = field(default_factory=CodexModelsConfig)
    codex_reasoning: CodexReasoningConfig = field(default_factory=CodexReasoningConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)
    deep_reviewer: DeepReviewerConfig = field(default_factory=DeepReviewerConfig)
    shadow: ShadowConfig = field(default_factory=ShadowConfig)
    golden: GoldenConfig = field(default_factory=GoldenConfig)
    address: AddressConfig = field(default_factory=AddressConfig)
    feedback: FeedbackConfig = field(default_factory=FeedbackConfig)
    run_pr: RunPrConfig = field(default_factory=RunPrConfig)


_FREEFORM = {(table, "per_language") for table in ("models", "codex_models", "codex_reasoning")}
# Keys that older versions accepted. Ignored so an existing config file keeps loading.
_REMOVED = {("review", "routing", "max_comment_percent"), ("review", "routing", "docs_min_comment_lines")}
# Keys (or whole tables) a repository's .code-check.toml may not set.
REPO_FORBIDDEN = {
    ("state_dir",), ("run_pr",), ("feedback",), ("shadow",), ("golden",), ("review", "auto_approve"), ("review", "attribution"), ("review", "label"), ("review", "login"), ("review", "ci_login"), ("review", "seed_comments"), ("review", "learn_guidance"), ("loop", "review_forks"),
    ("address", "test_command"), ("deep_reviewer", "command"),
    ("codex",), ("codex_models",), ("codex_reasoning",),
}
MODEL_NAME = re.compile(r"[A-Za-z0-9._:\[\]-]+")
CODEX_EFFORTS = ("low", "medium", "high")
LABEL_NAME = re.compile(r"[A-Za-z0-9._ :-]{1,50}")
# A GitHub login, optionally a bot: octocat, github-actions[bot]. Never starts with '-'.
LOGIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}(\[bot\])?")
# OWNER/REPO for `gh --repo`: neither part starts with '-' or '.'.
REPO_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}")
# Values accepted by `claude --permission-mode` (headless spike, docs/spikes/).
PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def expand_path(value: str) -> Path:
    return Path(value).expanduser()


def model_for(models: ModelsConfig | Config, role: str, language: str | None = None, *, runtime: str = "claude") -> str:
    if runtime == "claude":
        return _role_value(models.models if isinstance(models, Config) else models, role, language)
    if runtime == "codex" and isinstance(models, Config):
        return _codex_role_value(models.codex_models, role, language)
    raise ConfigError(f"cannot select a model for runtime {runtime!r} with this config")


def reasoning_for(cfg: Config, role: str, language: str | None = None) -> str:
    return _codex_role_value(cfg.codex_reasoning, role, language)


def _codex_role_value(models: ModelsConfig, role: str, language: str | None) -> str:
    return models.judge if role == "judge" else _role_value(models, role, language)


def _role_value(models: ModelsConfig, role: str, language: str | None) -> str:
    if role == "language_expert":
        if language and language in models.per_language:
            return models.per_language[language]
        return models.language_experts
    if role in ("aggregator", "reviewers", "deep", "security", "performance", "docs"):
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
        if here in _REMOVED:
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


def _reject_repo_keys(repo: Mapping) -> None:
    for key in sorted(REPO_FORBIDDEN):
        node = repo
        for part in key:
            if not isinstance(node, Mapping) or part not in node:
                break
            node = node[part]
        else:
            name = ".".join(key)
            raise ConfigError(
                f"config key {name} cannot be set in the repository's .code-check.toml (security-sensitive); "
                f"set it in ~/.config/code-check/config.toml or with a CODE_CHECK_ environment variable")


def _role_entries(table: ModelsConfig):
    yield from ((f.name, getattr(table, f.name)) for f in fields(table) if f.name != "per_language")
    yield from ((f"per_language.{k}", v) for k, v in table.per_language.items())


def validate_codex(cfg: Config) -> None:
    for key, value in _role_entries(cfg.codex_models):
        if not isinstance(value, str) or value.startswith("-") or not MODEL_NAME.fullmatch(value):
            raise ConfigError(f"config key codex_models.{key} is not a valid model name: {value!r}")
    for key, value in _role_entries(cfg.codex_reasoning):
        if value not in CODEX_EFFORTS:
            raise ConfigError(f"config key codex_reasoning.{key} must be one of {', '.join(CODEX_EFFORTS)}")
    for key in ("max_parallel", "timeout_seconds"):
        value = getattr(cfg.codex, key)
        if type(value) is not int or value < 1:
            raise ConfigError(f"config key codex.{key} must be a positive integer")


def validate(cfg: Config) -> Config:
    for key, value in _role_entries(cfg.models):
        if not isinstance(value, str) or not MODEL_NAME.fullmatch(value):
            raise ConfigError(f"config key models.{key} is not a valid model name: {value!r}")
    validate_codex(cfg)
    routing = cfg.review.routing
    if routing.mode not in ("auto", "off"):
        raise ConfigError("config key review.routing.mode must be \"auto\" or \"off\"")
    for key in ("small_max_lines", "small_max_files", "performance_min_score"):
        if getattr(routing, key) < 0:
            raise ConfigError(f"config key review.routing.{key} must not be negative")
    if cfg.review.quiet_after_round < 0:
        raise ConfigError("config key review.quiet_after_round must not be negative")
    label = cfg.review.label
    if label and (label.startswith("-") or not LABEL_NAME.fullmatch(label)):
        raise ConfigError(f"config key review.label is not a valid label name: {label!r}")
    login = cfg.review.login
    if login and not LOGIN_NAME.fullmatch(login):
        raise ConfigError(f"config key review.login is not a valid GitHub login: {login!r}")
    ci_login = cfg.review.ci_login
    if ci_login and not LOGIN_NAME.fullmatch(ci_login):
        raise ConfigError(f"config key review.ci_login is not a valid GitHub login: {ci_login!r}")
    seed = cfg.review.seed_comments
    if seed and (not os.path.isabs(os.path.expanduser(seed)) or seed.startswith("-") or "\x00" in seed):
        raise ConfigError(f"config key review.seed_comments must be an absolute file path: {seed!r}")
    for lic in cfg.golden.licenses:
        if not re.fullmatch(r"[a-z0-9.+-]{2,64}", lic):
            raise ConfigError(f"config key golden.licenses must be a list of license ids such as \"mit\"; got {lic!r}")
    fb = cfg.feedback
    if type(fb.min_interval_hours) is not int or fb.min_interval_hours < 1:
        raise ConfigError("config key feedback.min_interval_hours must be a positive integer")
    if fb.target_repo and not REPO_NAME.fullmatch(fb.target_repo):
        raise ConfigError(f"config key feedback.target_repo must look like OWNER/REPO: {fb.target_repo!r}")
    r = cfg.run_pr
    for key in ("review_allowed_tools", "babysit_allowed_tools"):
        for tool in getattr(r, key):
            if not tool.strip() or tool.lstrip().startswith("-"):
                raise ConfigError(f"config key run_pr.{key} has an invalid entry {tool!r} (must not start with '-')")
    if r.permission_mode not in PERMISSION_MODES:
        raise ConfigError(f"config key run_pr.permission_mode must be one of {', '.join(PERMISSION_MODES)}")
    for key in ("review_prompt", "babysit_prompt"):
        if getattr(r, key).lstrip().startswith("-"):
            raise ConfigError(f"config key run_pr.{key} must not start with '-'")
    return cfg


def load_config(repo_dir: Path, home: Path, env: Mapping[str, str]) -> Config:
    data = asdict(Config())
    repo = _read(repo_dir / ".code-check.toml")
    _reject_repo_keys(repo)
    _merge(data, repo)
    _merge(data, _read(home / ".config" / "code-check" / "config.toml"))
    _apply_env(data, env)
    return validate(_build(Config, data))
