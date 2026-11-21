"""Routing A/B: run two versions of the reviewer routing on every stored diff and list where the decision changed.

The old version is `core/routing.py` as it is at a git ref (default origin/main), loaded as a separate module; the
new version is the checkout's. No model, no tokens. It reads the diffs in the shadow store (`CODE_CHECK_SHADOW_STORE`,
default ~/.local/share/code-check/shadow), counts each distinct diff once, and prints or writes only counts, run ids
and routing reasons (signal names and scores, never code).

A flip from on to off means the new routing would skip a reviewer the old one ran; off to on means it adds one. Whether
a flip is right needs the reviewer's findings on that run, which this tool does not judge.

Usage: python -B evals/deepeval/routing_ab.py [--base-ref origin/main] [--json FILE]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType

from anchoring import STORE

from codecheck.config import RoutingConfig
from codecheck.core.diffmap import parse_diff
from codecheck.core.panel import detect_language
from codecheck.core.routing import route as new_route

REPO = Path(__file__).resolve().parents[2]


def load_routing(ref: str, repo: Path = REPO) -> ModuleType:
    """`core/routing.py` as it is at `ref`, executed as a separate module so it can run next to the current one."""
    if not ref or ref.startswith("-"):
        raise ValueError(f"not a usable git ref: {ref!r}")
    source = subprocess.run(  # noqa: S603  (argv list; the ref was checked above and never starts with "-")
        ["git", "-C", str(repo), "show", f"{ref}:src/codecheck/core/routing.py"],  # noqa: S607
        capture_output=True, text=True, check=True,
    ).stdout
    name = "routing_at_" + hashlib.sha256(ref.encode()).hexdigest()[:12]
    module = ModuleType(name)
    sys.modules[name] = module  # the module's dataclasses look themselves up here while the class body runs
    exec(compile(source, f"routing@{ref}", "exec"), module.__dict__)  # noqa: S102  (code from this repository at a ref the maintainer chose)
    return module


def _decide(diff_text: str, route: Callable, cfg: RoutingConfig):
    diffs = {p: d for p, d in parse_diff(diff_text).items() if not d.binary and d.status != "deleted"}
    languages = {p: detect_language(p, "") for p in diffs}
    return len(diffs), route(diffs, languages, cfg)


def compare(diffs_by_run: Mapping[str, str], old_route: Callable, new_route: Callable, cfg: RoutingConfig) -> dict:
    """Route each distinct diff with both versions. Returns `summary` counts, the `flips` in performance routing and all `rows`."""
    seen: set[str] = set()
    rows: list[dict] = []
    for run, text in sorted(diffs_by_run.items()):
        digest = hashlib.sha256(text.encode()).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        try:
            files, old = _decide(text, old_route, cfg)
            _, new = _decide(text, new_route, cfg)
        except Exception as e:  # noqa: BLE001  (one odd diff must not stop the sweep)
            rows.append({"run": run, "error": repr(e)[:80]})
            continue
        rows.append({
            "run": run, "files": files,
            "old_performance": old.performance, "new_performance": new.performance,
            "old_architect": old.architect, "new_architect": new.architect,
            "old_docs": old.docs, "new_docs": new.docs,
            "old_reason": old.reasons[1], "new_reason": new.reasons[1],
        })
    ok = [r for r in rows if "error" not in r]
    summary: Counter = Counter()
    summary["distinct diffs"] = len(ok)
    summary["errors"] = len(rows) - len(ok)
    for r in ok:
        summary[f"performance: old={'on' if r['old_performance'] else 'off'} new={'on' if r['new_performance'] else 'off'}"] += 1
        summary["architect changed"] += r["old_architect"] != r["new_architect"]
        summary["docs changed"] += r["old_docs"] != r["new_docs"]
    flips = [r for r in ok if r["old_performance"] != r["new_performance"]]
    return {"summary": dict(sorted(summary.items())), "flips": flips, "rows": ok}


def main() -> None:
    ap = argparse.ArgumentParser(description="A/B two versions of the reviewer routing on stored diffs.")
    ap.add_argument("--base-ref", default="origin/main", help="git ref holding the old routing (default origin/main)")
    ap.add_argument("--json", type=Path, help="write the summary, flips and all rows here")
    args = ap.parse_args()
    diffs = {p.stem: p.read_text(errors="replace") for p in sorted(STORE.glob("*.diff"))}
    result = compare(diffs, load_routing(args.base_ref).route, new_route, RoutingConfig())
    print(json.dumps(result["summary"], indent=2))
    print(f"{len(result['flips'])} performance flips")
    if args.json:
        args.json.write_text(json.dumps(result, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
