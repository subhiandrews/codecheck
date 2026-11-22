"""Free-form finding categories to a small canonical set. Anything unknown is `other`."""

CANONICAL = (
    "correctness", "error-handling", "validation", "tests", "docs", "security", "performance",
    "compatibility", "api-design", "concurrency", "build", "maintainability",
)
ANY = "any"  # the category of a miss: a human comment has none
OTHER = "other"

_ALIASES = {
    "correctness": ("correctness", "bug", "logic", "behavior", "behaviour", "behavior-change", "edge-case", "edge-cases",
                    "invariant", "side-effect", "boundaries", "boundary", "completeness", "regression"),
    "error-handling": ("error-handling", "errors", "error", "error-message", "panic-safety", "exceptions", "exception"),
    "validation": ("validation", "input-validation", "sanitization", "sanitisation"),
    "tests": ("tests", "test", "testing", "test-coverage", "coverage"),
    "docs": ("docs", "doc", "documentation", "comments", "comment"),
    "security": ("security", "vulnerability", "auth", "authz", "injection"),
    "performance": ("performance", "perf", "efficiency", "memory"),
    "compatibility": ("compatibility", "compat", "portability", "backwards-compat"),
    "api-design": ("api-design", "api", "interface", "naming"),
    "concurrency": ("concurrency", "race", "race-condition", "locking", "async", "cancellation", "deadlock"),
    "build": ("build", "ci", "dependency", "dependencies", "packaging", "release"),
    "maintainability": ("maintainability", "convention", "consistency", "simplicity", "design", "style",
                        "readability", "duplication", "refactor"),
}
_LOOKUP = {alias: canon for canon, aliases in _ALIASES.items() for alias in aliases}


def canonical_category(raw: object) -> str:
    if not isinstance(raw, str):
        return OTHER
    key = "-".join(raw.lower().replace("_", " ").replace("/", " ").split())
    return _LOOKUP.get(key, OTHER)
