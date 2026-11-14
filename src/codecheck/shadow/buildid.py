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
        digest.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:12]


def plugin_build(env: Mapping[str, str], *, default_root: Path | None = _REPO_ROOT) -> dict:
    """`plugin_root`, `prompts_hash` and `build_reason` (why the first two are null); never raises."""
    none = {"plugin_root": None, "prompts_hash": None}
    try:
        configured = env.get("CLAUDE_PLUGIN_ROOT")
        if configured:
            root = Path(configured).expanduser()
            if not root.is_dir():
                return {**none, "build_reason": "CLAUDE_PLUGIN_ROOT is not a directory"}
        elif default_root is not None and (default_root / "agents").is_dir() and (default_root / ".claude-plugin" / "plugin.json").is_file():
            root = default_root
        else:
            return {**none, "build_reason": "CLAUDE_PLUGIN_ROOT is not set and the CLI is not running from a plugin checkout"}
        root = root.resolve()
        return {"plugin_root": str(root), "prompts_hash": prompts_hash(root), "build_reason": None}
    except OSError as e:
        return {**none, "build_reason": f"{type(e).__name__}: {e}"[:200]}
