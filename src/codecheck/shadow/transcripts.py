"""Find and summarise Claude Code session transcripts for a run. Output is always scrubbed."""

import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path

from codecheck.shadow.scrub import scrub_text

_ERROR_CAP = 300
_GUIDANCE_CAP = 20  # entries; each is cut to _ERROR_CAP characters
_GUIDANCE = "ignored guidance:"


def find_transcript(
    projects_dir: Path,
    run_id: str,
    *,
    max_age_s: float = 86400,
    now: Callable[[], float] = time.time,
) -> Path | None:
    try:
        cutoff = now() - max_age_s
        candidates = []
        for p in projects_dir.glob("*/*.jsonl"):
            try:
                if p.is_file() and not p.is_symlink() and (m := p.stat().st_mtime) >= cutoff:
                    candidates.append((m, p))
            except OSError:
                continue
        needle = run_id.encode("utf-8")
        for _, p in sorted(candidates, key=lambda c: c[0], reverse=True):
            try:
                if needle in p.read_bytes():
                    return p
            except OSError:
                continue
    except OSError:
        return None
    return None


def subagent_transcripts(main: Path) -> list[Path]:
    folder = main.with_suffix("") / "subagents"
    try:
        return sorted(folder.glob("*.jsonl"))
    except OSError:
        return []


def belongs_to_run(path: Path, run_id: str) -> bool:
    """True when the file's first `user` entry is a string naming this run's directory (`RUN_DIR=<state>/runs/<run_id>`)."""
    needle = f"runs/{run_id}"
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if not (isinstance(entry, dict) and entry.get("type") == "user"):
                    continue
                msg = entry.get("message")
                content = msg.get("content") if isinstance(msg, dict) else None
                return isinstance(content, str) and needle in content
    except OSError:
        return False
    return False


def _epoch(entry: dict) -> float | None:
    ts = entry.get("timestamp")
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in content
        )
    return "" if content is None else str(content)


def _guidance_lines(text: str, found: list[str]) -> None:
    """Collect lines reporting ignored guidance (an agent's `done:` line), scrubbed, capped and without repeats."""
    for line in text.splitlines():
        if len(found) >= _GUIDANCE_CAP:
            return
        if _GUIDANCE in line.lower():
            entry = scrub_text(line.strip())[:_ERROR_CAP]
            if entry not in found:
                found.append(entry)


def extract_signals(paths: Sequence[Path], *, windows: Mapping[Path, tuple[float, float]] | None = None) -> dict:
    """Deterministic counts from transcripts. `ignored_guidance` comes only from assistant text and tool results,
    never from thinking blocks or user prompts. For a path in `windows`, only entries whose `timestamp` lies in the
    closed (start, end) epoch-seconds window count; entries without a usable timestamp are skipped."""
    out = {
        "tool_calls": 0, "tool_errors": [], "retries": 0,
        "permission_denials": 0, "agents": [], "unreadable_lines": 0, "ignored_guidance": [],
    }
    for path in paths:
        agent = {"name": path.stem, "tool_calls": 0, "tool_errors": 0, "turns": 0, "output_tokens": 0}
        uses: dict[str, tuple[str, str]] = {}
        last_error: tuple[str, str] | None = None
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            out["unreadable_lines"] += 1
            out["agents"].append(agent)
            continue
        window = (windows or {}).get(path)
        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                if not isinstance(entry, dict):
                    raise TypeError("not an object")
                if window is not None and not ((at := _epoch(entry)) is not None and window[0] <= at <= window[1]):
                    continue
                kind = entry.get("type")
                msg = entry.get("message")
                blocks = msg.get("content") if isinstance(msg, dict) else None
                if kind == "assistant":
                    agent["turns"] += 1
                    usage = msg.get("usage") if isinstance(msg, dict) else None
                    tokens = usage.get("output_tokens") if isinstance(usage, dict) else 0
                    agent["output_tokens"] += tokens if isinstance(tokens, int) else 0
                    for b in blocks if isinstance(blocks, list) else []:
                        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
                            _guidance_lines(b["text"], out["ignored_guidance"])
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            key = (str(b.get("name")), json.dumps(b.get("input"), sort_keys=True))
                            agent["tool_calls"] += 1
                            out["tool_calls"] += 1
                            if key == last_error:
                                out["retries"] += 1
                            last_error = None
                            uses[str(b.get("id"))] = key
                elif kind == "user":
                    for b in blocks if isinstance(blocks, list) else []:
                        if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                            continue
                        _guidance_lines(_result_text(b.get("content")), out["ignored_guidance"])
                        if not b.get("is_error"):
                            continue
                        key = uses.get(str(b.get("tool_use_id")), ("unknown", ""))
                        text = scrub_text(_result_text(b.get("content")))
                        agent["tool_errors"] += 1
                        out["tool_errors"].append(
                            {"agent": path.stem, "tool": key[0], "error": text[:_ERROR_CAP]})
                        if "permission" in text.lower():
                            out["permission_denials"] += 1
                        last_error = key
            except (ValueError, AttributeError, TypeError, RecursionError):  # RecursionError: absurdly deep JSON
                out["unreadable_lines"] += 1
        out["agents"].append(agent)
    return out


def copy_scrubbed(paths: Sequence[Path], dest: Path) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(dest, 0o700)
    written = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        target = dest / path.name
        if target.is_symlink():
            target.unlink()
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            f = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            raise
        with f:
            os.fchmod(f.fileno(), 0o600)  # the descriptor, not the path: nothing can swap the file in between
            f.write(scrub_text(text).encode("utf-8"))
        written.append(path.name)
    return written
