"""Tolerant matching of a live review's findings against a snippet's expected findings.

Live reviews vary run to run, so a finding matches an expected one when the file is equal, the category is one of
the expected names (compared case-insensitively; reviewers pick categories freely, so an expected `category` may
be a list of acceptable names) and the line is within TOLERANCE. The severity must then be equal for the finding to
count. A case passes at RECALL_BAR recall with no forbidden finding and no unexpected Critical. The pass bar and
the tolerance live here and nowhere else.
"""

from collections.abc import Sequence

TOLERANCE = 2
RECALL_BAR = 0.8


def _names(category) -> set[str]:
    values = [category] if isinstance(category, str) else list(category or [])
    return {str(v).strip().lower() for v in values}


def _category_ok(wanted, got: dict) -> bool:
    return str(got.get("category", "")).strip().lower() in _names(wanted)


def _sev(item: dict) -> str:
    return str(item.get("severity", "")).strip().lower()


def _near(a, b, tolerance: int) -> bool:
    return isinstance(a, int) and isinstance(b, int) and abs(a - b) <= tolerance


def _place(exp: dict, got: dict, tolerance: int) -> bool:
    return got.get("file") == exp.get("file") and _category_ok(exp.get("category"), got) and \
        _near(got.get("line"), exp.get("line"), tolerance)


def match_case(expected: Sequence[dict], must_not_report: Sequence[dict], got: Sequence[dict], *,
               tolerance: int = TOLERANCE, recall_bar: float = RECALL_BAR) -> dict:
    used: set[int] = set()

    def take(exp: dict, same_severity: bool) -> int | None:
        candidates = [i for i, g in enumerate(got) if i not in used and _place(exp, g, tolerance)
                      and (_sev(g) == _sev(exp)) == same_severity]
        if not candidates:
            return None
        best = min(candidates, key=lambda i: abs(got[i]["line"] - exp["line"]))  # nearest line, then first
        used.add(best)
        return best

    # Right-severity matches first, so a nearer wrong-severity finding cannot take an expected item's place.
    hits = {e: take(exp, True) for e, exp in enumerate(expected)}
    mismatches = []
    for e, exp in enumerate(expected):
        if hits[e] is None and (i := take(exp, False)) is not None:
            mismatches.append({"expected": exp, "got": got[i]})
    matched = sum(1 for i in hits.values() if i is not None)
    recall = matched / len(expected) if expected else 1.0

    forbidden = []
    for rule in must_not_report:
        for g in got:
            if g.get("file") != rule.get("file"):
                continue
            if rule.get("category") is not None and not _category_ok(rule["category"], g):
                continue
            if rule.get("severity") is not None and _sev(g) not in _names(rule["severity"]):
                continue
            if rule.get("line") is not None and not _near(g.get("line"), rule["line"], tolerance):
                continue
            forbidden.append({"rule": rule, "got": g})
    unexpected_critical = [g for i, g in enumerate(got) if i not in used and _sev(g) == "critical"]

    return {"passed": recall >= recall_bar and not forbidden and not unexpected_critical,
            "matched": matched, "expected": len(expected), "recall": round(recall, 3),
            "severity_mismatches": mismatches, "unexpected_critical": unexpected_critical, "forbidden_hits": forbidden}
