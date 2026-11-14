"""Which plugin build produced a run: the plugin root and a short hash of its agent and skill prompts."""

import hashlib
from collections.abc import Mapping
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _prompt_files(root: Path) -> list[Path]:
    found = list((root / "agents").glob("*.md")) + list((root / "skills").glob("*/SKILL.md"))
    return sorted((p for p in found if p.is_file() and not p.is_symlink()), key=lambda p: p.relative_to(root).as_posix())


def prompts_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in _prompt_files(root):
        digest.update(path.relative_to(root).as_posix().encode("utf-8", "surrogateescape") + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:12]


def _is_plugin_root(root: Path) -> bool:
    return (root / "agents").is_dir() and (root / ".claude-plugin" / "plugin.json").is_file()


def plugin_build(env: Mapping[str, str], *, default_root: Path | None = _REPO_ROOT) -> dict:
    """`plugin_root`, `prompts_hash` and `build_reason` (why the first two are null); never raises.

    The root is `CODE_CHECK_PLUGIN_ROOT` (exported by `bin/code-check`), else `CLAUDE_PLUGIN_ROOT`, else the checkout
    this module sits in. Any root must hold `agents/` and `.claude-plugin/plugin.json`.
    """
    none = {"plugin_root": None, "prompts_hash": None}
    try:
        name = next((n for n in ("CODE_CHECK_PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT") if env.get(n)), None)
        if name:
            root = Path(env[name]).expanduser()
            if not root.is_dir():
                return {**none, "build_reason": f"{name} is not a directory"}
            if not _is_plugin_root(root):
                return {**none, "build_reason": f"{name} is not a plugin root (no agents/ or .claude-plugin/plugin.json)"}
        elif default_root is not None and _is_plugin_root(default_root):
            root = default_root
        else:
            return {**none, "build_reason": "CODE_CHECK_PLUGIN_ROOT and CLAUDE_PLUGIN_ROOT are not set and the CLI is not running from a plugin checkout"}
        root = root.resolve()
        return {"plugin_root": str(root), "prompts_hash": prompts_hash(root), "build_reason": None}
    except Exception as e:  # noqa: BLE001 - recording the build must never break a run
        return {**none, "build_reason": f"{type(e).__name__}: {e}"[:200]}
