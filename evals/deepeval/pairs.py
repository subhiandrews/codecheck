"""Human-comment / candidate pairs for the question "is this the same concern?".

pairs.json holds only references and labels. The text is resolved from tests/golden/*.json
(read-only), so it cannot drift from the golden cases. Each pair has my reading (`my_label`) and an
`owner_label` that stays null until a person corrects it. Controls are pairs that should be
"different": a judge that says "same" to them is saying yes to everything.

Usage: .venv/bin/python -B evals/deepeval/pairs.py > review.md   (prints a sheet for reading)
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
GOLDEN = HERE.parents[1] / "tests" / "golden"
LABELS = ("same", "related", "different")
STAGES = ("kept", "dropped", "raw")


def _pool(case: dict, stage: str) -> list[dict]:
    if stage == "kept":
        return case["aggregator"]["findings"]
    if stage == "dropped":
        return case["aggregator"]["dropped"]
    if stage == "raw":
        return [f for findings in case["reviewer_outputs"].values() for f in findings]
    raise ValueError(f"unknown stage {stage!r}")


def resolve(pair: dict) -> dict:
    """Attach the human comment and candidate text. Raises LookupError unless each ref is unique."""
    case = json.loads((GOLDEN / f"{pair['case_id']}.json").read_text())
    humans = [m for m in case["misses"] if m["line"] == pair["human_line"]]
    ref = pair["candidate"]
    found = [
        c
        for c in _pool(case, ref["stage"])
        if c["line"] == ref["line"] and Path(c["file"]).name == ref["file"]
    ]
    if len(humans) != 1 or len(found) != 1:
        raise LookupError(f"{pair['id']}: {len(humans)} human match(es), {len(found)} candidate match(es)")
    return {
        **pair,
        "human_path": humans[0]["path"],
        "human_text": humans[0]["body"],
        "candidate_file": found[0]["file"],
        "candidate_text": found[0]["message"],
    }


def load_pairs() -> list[dict]:
    return [resolve(p) for p in json.loads((HERE / "pairs.json").read_text())]


def render(pairs: list[dict]) -> str:
    out = ["# Is the candidate the same concern as the human comment?", ""]
    out += [
        "Labels: **same** (one concern), **related** (same area or cause, a different ask or mechanism),",
        "**different**. Reply with the pair id and your label, for example `P3 same`. Controls (C1, C2) should",
        "be different.",
        "",
    ]
    for p in pairs:
        stage = p["candidate"]["stage"]
        out += [
            f"## {p['id']}{' (control)' if p['control'] else ''} · {p['case_id']}",
            "",
            f"**Human comment** at `{p['human_path']}:{p['human_line']}`:",
            "",
            *[f"> {line}" for line in p["human_text"].strip().splitlines()],
            "",
            f"**Candidate** ({stage} at `{p['candidate_file']}:{p['candidate']['line']}`):",
            "",
            *[f"> {line}" for line in p["candidate_text"].strip().splitlines()],
            "",
            f"**My reading:** {p['my_label']} ({p['my_confidence']} confidence). {p['my_reason']}",
            "",
            f"**Yours:** {p['owner_label'] or '…'}",
            "",
        ]
    return "\n".join(out)


if __name__ == "__main__":
    print(render(load_pairs()))
