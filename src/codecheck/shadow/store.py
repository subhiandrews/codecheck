"""Append-only, schema-versioned JSONL store: one file per run."""

import json
import time
from collections.abc import Callable
from pathlib import Path

from codecheck.runids import check_run_id
from codecheck.shadow.scrub import scrub, scrub_text

SCHEMA = 1


class ShadowStore:
    def __init__(self, root: Path, now: Callable[[], float] = time.time) -> None:
        self.root = Path(root)
        self.now = now

    def _path(self, run_id: str, suffix: str) -> Path:
        return self.root / f"{check_run_id(run_id)}{suffix}"

    def append(self, run_id: str, record: dict) -> None:
        rec = scrub({"schema": SCHEMA, "ts": self.now(), "run_id": run_id, **record})
        path = self._path(run_id, ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")

    def read(self, run_id: str) -> list[dict]:
        path = self._path(run_id, ".jsonl")
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def run_ids(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.jsonl")) if self.root.exists() else []

    def save_diff(self, run_id: str, diff: str) -> None:
        path = self._path(run_id, ".diff")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(scrub_text(diff), encoding="utf-8")

    def read_diff(self, run_id: str) -> str:
        path = self._path(run_id, ".diff")
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def runs_for_pr(self, pr: str) -> list[str]:
        return [
            rid for rid in self.run_ids()
            if any(r.get("type") == "run" and r.get("pr") == pr for r in self.read(rid))
        ]
