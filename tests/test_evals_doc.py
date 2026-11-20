"""docs/Evals.md explains how the evals fit together. It names flags, files, constants, counts and config keys; this
file fails when one of them no longer matches the code, so the page cannot quietly go stale."""

import json
import re
from dataclasses import fields, is_dataclass
from pathlib import Path

import pytest

from codecheck.cli import (
    build_parser,  # first: importing evals.judge before the CLI is a circular import
)
from codecheck.config import Config
from codecheck.evals.judge import AGREEMENT_BAR
from codecheck.evals.match import RECALL_BAR, TOLERANCE

REPO = Path(__file__).resolve().parent.parent
DOC = (REPO / "docs" / "Evals.md").read_text()
WORDS = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five", 6: "Six", 7: "Seven", 8: "Eight", 9: "Nine"}


def _word(n: int) -> str:
    """The count as the page writes it at the start of a sentence; a count past nine is a digit and fails the
    assertion with a message that names it, not a KeyError."""
    return WORDS.get(n, str(n))


def _flags(command: str | None = None) -> set[str]:
    """The option strings of one subcommand, or of every subcommand (and its nested ones) when none is given."""
    sub = next(a for a in build_parser()._actions if getattr(a, "choices", None) and "eval" in a.choices)

    def walk(parser):
        for a in parser._actions:
            yield from a.option_strings
            if isinstance(getattr(a, "choices", None), dict):  # a nested subcommand group, such as `shadow promote`
                for child in a.choices.values():
                    yield from walk(child)

    return set(walk(sub.choices[command])) if command else {f for p in sub.choices.values() for f in walk(p)}


def test_every_eval_flag_the_page_names_exists():
    after_eval = set(re.findall(r"eval (--[a-z-]+)", DOC))
    quoted = set(re.findall(r"`(--[a-z-]+)`", DOC))
    assert after_eval <= _flags("eval"), after_eval - _flags("eval")
    assert quoted <= _flags(), quoted - _flags()
    assert {"--replay", "--live", "--calibrate", "--history", "--no-record"} <= after_eval | quoted  # every eval is covered


def test_every_repository_path_the_page_names_exists():
    paths = set(re.findall(r"`((?:src|tests|docs|scripts)/[^`\s]+|README\.md)`", DOC))
    assert len(paths) >= 8
    assert [p for p in paths if not (REPO / p).exists()] == []


def test_the_pass_bars_and_the_tolerance_match_the_code():
    assert f"within {TOLERANCE} (`TOLERANCE`" in DOC
    assert f"at least {round(RECALL_BAR * 100)}% of its expected findings match (`RECALL_BAR`)" in DOC
    assert f"passes at {round(AGREEMENT_BAR * 100)}% agreement (`AGREEMENT_BAR`" in DOC


def test_the_counts_of_golden_cases_and_snippets_match_the_repository():
    cases = [json.loads(p.read_text()) for p in sorted((REPO / "tests/golden").glob("*.json"))]
    snippets = [json.loads(p.read_text()) for p in sorted((REPO / "tests/snippets").glob("*/case.json"))]
    hand_built = [c for c in cases if c["case_id"] == "seed-pipeline"]
    calibrated = [s for s in snippets if s.get("calibrated") is True]
    assert len(hand_built) == 1  # the page says exactly one case is hand-built
    assert f"**{len(cases)} golden cases**" in DOC and f"**{len(snippets)} snippets**" in DOC
    assert f"{len(cases)} golden cases and {len(snippets)} snippets" in DOC
    assert f"{_word(len(cases) - len(hand_built))} come from real pull requests" in DOC
    assert f"{_word(len(calibrated))} are marked `\"calibrated\": true`" in DOC


def test_the_page_names_every_snippet_that_ships():
    shipped = {p.parent.name for p in (REPO / "tests/snippets").glob("*/case.json")}
    assert shipped and [f"`{name}`" for name in sorted(shipped) if f"`{name}`" not in DOC] == []


def test_the_config_keys_the_page_names_exist():
    def keys(cls, prefix=""):
        for f in fields(cls):
            value = getattr(cls(), f.name)
            if is_dataclass(value):
                yield from keys(type(value), f"{prefix}{f.name}.")
            else:
                yield f"{prefix}{f.name}"

    known = set(keys(Config))
    named = set(re.findall(r"`((?:models|shadow)\.[a-z_]+)(?:`| =)", DOC))
    assert {"models.judge", "shadow.handoff"} <= named <= known, named - known


def test_the_claims_about_who_records_and_who_labels_hold():
    assert "--no-record" in (REPO / "scripts/gates.sh").read_text()
    skill = (REPO / ".claude/skills/handoff-round/SKILL.md").read_text()
    assert "--no-record" in skill and "labelled_by" in skill and '"handoff-round"' in skill
    assert "run_replay" in (REPO / "src/codecheck/commands/handoff_queue.py").read_text()  # the merge guard replays
    assert "merge=union" in (REPO / ".gitattributes").read_text()


def test_the_diagram_is_there_and_its_class_lists_name_real_nodes():
    blocks = re.findall(r"```mermaid\n(.*?)```", DOC, re.DOTALL)
    assert len(blocks) == 1 and blocks[0].startswith("flowchart")
    diagram = blocks[0]
    ids = set(re.findall(r"^\s*([a-z]\w*)(?:\[|\()", diagram, re.MULTILINE)) | set(re.findall(r"-->\s*([a-z]\w*)(?:\[|\()", diagram))
    classed = {n for line in re.findall(r"^\s*class ([\w,]+) \w+$", diagram, re.MULTILINE) for n in line.split(",")}
    assert classed and classed <= ids, classed - ids
    for command in ("eval --replay", "eval --live", "eval --calibrate"):
        assert command in diagram


def test_the_readme_points_at_the_page_and_the_page_points_at_the_decision_records():
    assert "docs/Evals.md" in (REPO / "README.md").read_text()
    for record in ("0010-replay-gates-every-commit-live-eval-is-manual.md", "0023-the-llm-judge-is-advisory-only.md"):
        assert f"{record}" in DOC and (REPO / "docs" / "decisions" / record).is_file()


@pytest.mark.parametrize("section", ["The picture", "The four evals", "Where the data comes from",
                                     "How they fit into the workflow", "Design decisions", "Reading the numbers",
                                     "Known limits", "Where to read more"])
def test_the_page_keeps_its_sections(section):
    assert f"## {section}" in DOC
