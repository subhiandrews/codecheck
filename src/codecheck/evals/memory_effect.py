"""Does memory help a review? `eval --memory-effect` runs each snippet that carries `memory` lessons twice through the live
runner, once with memory off and once with the seeded lessons on, and reports what changed. Local only and costs
tokens (two full reviews per snippet); advisory, never a gate (decision 0023, 0044).

The snippet's top-level `expected` and `must_not_report` are the memory-on expectations (they are also what
`eval --live` checks). `memory_effect.off` may override either list for the memory-off arm. For each pair the result says
whether the memory-on arm met its expectations (`as_expected`; `baseline_passed` is the memory-off arm against its own) and what memory changed: findings it `suppressed` (a forbidden finding that the off
arm reported and the on arm did not), findings it `surfaced` (expected, found only with memory) and expected findings it
`lost` (found only without memory, the harm a poisoned lesson could do).
"""

import tempfile
from collections.abc import Callable
from pathlib import Path

from codecheck.errors import CodeCheckError
from codecheck.evals.live import load_snippets, run_snippet, unreachable_reason
from codecheck.evals.match import match_case

DEFAULT_ARM_KEYS = ("expected", "must_not_report")


def memory_snippets(directory: Path, only: str | None = None) -> list[dict]:
    snippets = [s for s in load_snippets(directory) if s.get("memory")]
    if only is not None:
        names = [s["name"] for s in snippets]
        if only not in names:
            raise CodeCheckError(f"unknown memory snippet {only!r}; known: {', '.join(names) or 'none'}")
        snippets = [s for s in snippets if s["name"] == only]
    return snippets


def off_arm(snippet: dict) -> dict:
    """The snippet without its lessons, and with the memory-off expectations when it gives any."""
    override = (snippet.get("memory_effect") or {}).get("off") or {}
    arm = {k: v for k, v in snippet.items() if k not in ("memory", "memory_effect")}
    for key in DEFAULT_ARM_KEYS:
        if key in override:
            arm[key] = override[key]
    return arm


def _found(expected: dict, got: list[dict]) -> bool:
    return match_case([expected], [], got)["matched"] == 1


def _forbidden(rule: dict, got: list[dict]) -> bool:
    return bool(match_case([], [rule], got)["forbidden_hits"])


def compare(snippet: dict, off_got: list[dict], on_got: list[dict]) -> dict:
    """What turning memory on changed, judged against the memory-on expectations."""
    expected, rules = snippet["expected"], snippet["must_not_report"]
    return {
        "suppressed": [r for r in rules if _forbidden(r, off_got) and not _forbidden(r, on_got)],
        "still_reported": [r for r in rules if _forbidden(r, on_got)],
        "surfaced": [e for e in expected if _found(e, on_got) and not _found(e, off_got)],
        "lost": [e for e in expected if _found(e, off_got) and not _found(e, on_got)],
    }


def run_pair(ctx, snippet: dict, work: Path, run: Callable = run_snippet) -> dict:
    """The memory-off review, then the memory-on review, of one snippet; each in its own work directory."""
    off_dir, on_dir = work / "off", work / "on"
    off_dir.mkdir()
    on_dir.mkdir()
    off = run(ctx, off_arm(snippet), off_dir)
    on = run(ctx, snippet, on_dir)
    effect = compare(snippet, off["got"], on["got"])
    cost = sum(a["claude"]["cost_usd"] or 0 for a in (off, on))
    return {
        "name": snippet["name"], "off": off["result"] | {"error": off.get("error")}, "on": on["result"] | {"error": on.get("error")},
        "off_got": off["got"], "on_got": on["got"], "effect": effect,
        "baseline_passed": bool(off["result"]["passed"]),
        "as_expected": bool(on["result"]["passed"] and not effect["lost"]),
        "cost_usd": round(cost, 4),
    }


def run_effect(ctx, directory: Path, only: str | None, if_reachable: bool, run: Callable = run_snippet) -> dict:
    snippets = memory_snippets(directory, only)
    base = {"advisory": True, "pairs": []}
    if not snippets:
        return base | {"as_expected": 0, "total": 0}
    reason = unreachable_reason(ctx)
    if reason is not None:
        if if_reachable:
            return base | {"skipped": f"model not reachable: {reason}"}
        raise CodeCheckError(f"model not reachable: {reason}")
    pairs = []
    for snippet in snippets:  # one pair after another, never retried: each review costs real tokens
        with tempfile.TemporaryDirectory(prefix="code-check-memory-effect-", ignore_cleanup_errors=True) as work:
            pairs.append(run_pair(ctx, snippet, Path(work), run))
    return base | {"pairs": pairs, "as_expected": sum(p["as_expected"] for p in pairs), "total": len(pairs),
                   "cost_usd": round(sum(p["cost_usd"] for p in pairs), 4)}
