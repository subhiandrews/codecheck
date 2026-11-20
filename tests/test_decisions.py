import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("decisions", ROOT / "scripts" / "decisions.py")
decisions = importlib.util.module_from_spec(_spec)
sys.modules["decisions"] = decisions
_spec.loader.exec_module(decisions)

SUPERSEDED = "superseded by {} (accepted 2026-11-12)"


def record(num="0001", title="Short title", status="accepted 2026-11-12", decision="Do the thing.", why="Because.",
           rejected="Nothing else.", evidence=None):
    lines = [f"# {num} {title}", f"Status: {status}", f"Decision: {decision}", f"Why: {why}", f"Rejected: {rejected}"]
    if evidence:
        lines.append(f"Evidence: {evidence}")
    return "\n".join(lines) + "\n"


def make_dir(tmp_path, records, name="decisions"):
    d = tmp_path / name
    d.mkdir()
    for fname, text in records.items():
        (d / fname).write_text(text)
    (d / "README.md").write_text("# Decisions\n\n" + "\n".join(f"- [{n}]({n})" for n in records) + "\n")
    return d


def test_the_real_decision_records_pass_every_check():
    assert decisions.problems(ROOT / "docs" / "decisions") == []


def test_the_status_line_is_not_counted_and_everything_else_is():
    text = record(title="One two", decision="three four", why="five", rejected="six")
    # "# 0001 One two" (4) + "Decision: three four" (3) + "Why: five" (2) + "Rejected: six" (2)
    assert decisions.words(text) == 11
    assert decisions.words(record(status=SUPERSEDED.format("0002"))) == decisions.words(record())


def test_exactly_100_words_pass_and_101_fail(tmp_path):
    base = decisions.words(record(decision=""))
    ok = record(decision=" ".join(["w"] * (100 - base)))
    too_long = record(decision=" ".join(["w"] * (101 - base)))
    assert decisions.words(ok) == 100 and decisions.words(too_long) == 101
    assert decisions.problems(make_dir(tmp_path, {"0001-short-title.md": ok}, "ok")) == []
    out = decisions.problems(make_dir(tmp_path, {"0001-short-title.md": too_long}, "long"))
    assert any("101 words" in p and "limit 100" in p for p in out)


@pytest.mark.parametrize("field", ["Decision", "Why", "Rejected"])
def test_a_missing_required_field_is_reported(tmp_path, field):
    text = "\n".join(line for line in record().splitlines() if not line.startswith(f"{field}:")) + "\n"
    out = decisions.problems(make_dir(tmp_path, {"0001-short-title.md": text}))
    assert any(field in p for p in out)


def test_a_bad_status_line_is_reported(tmp_path):
    out = decisions.problems(make_dir(tmp_path, {"0001-short-title.md": record(status="maybe")}))
    assert any("Status" in p for p in out)


def test_the_file_name_must_match_the_number_in_the_title(tmp_path):
    out = decisions.problems(make_dir(tmp_path, {"0002-short-title.md": record(num="0001")}))
    assert any("0002-short-title.md" in p for p in out)


def test_numbers_must_be_contiguous_from_one(tmp_path):
    d = make_dir(tmp_path, {"0001-a.md": record("0001"), "0003-c.md": record("0003")})
    assert any("0002" in p for p in decisions.problems(d))


def test_a_supersession_must_point_at_a_later_existing_record(tmp_path):
    missing = {"0001-a.md": record("0001", status=SUPERSEDED.format("0009"))}
    assert any("0009" in p for p in decisions.problems(make_dir(tmp_path, missing, "missing")))
    backwards = {"0001-a.md": record("0001"), "0002-b.md": record("0002", status=SUPERSEDED.format("0001"))}
    assert any("0001" in p for p in decisions.problems(make_dir(tmp_path, backwards, "backwards")))
    good = {"0001-a.md": record("0001", status=SUPERSEDED.format("0002")), "0002-b.md": record("0002")}
    assert decisions.problems(make_dir(tmp_path, good, "good")) == []


def test_an_evidence_link_must_exist(tmp_path):
    out = decisions.problems(make_dir(tmp_path, {"0001-a.md": record(evidence="docs/spikes/nope.md")}))
    assert any("nope.md" in p for p in out)


def test_the_index_must_list_every_record(tmp_path):
    d = make_dir(tmp_path, {"0001-a.md": record()})
    (d / "README.md").write_text("# Decisions\n")
    assert any("0001-a.md" in p and "README" in p for p in decisions.problems(d))


def run_new(d, **kw):
    argv = ["new", "--dir", str(d), "--title", "New thing", "--date", "2026-11-20", "--decision", "Do it.",
            "--why", "It helps.", "--rejected", "Not doing it."]
    for key, value in kw.items():
        argv += [f"--{key.replace('_', '-')}", value]
    return decisions.main(argv)


def test_new_writes_the_next_numbered_record_and_indexes_it(tmp_path):
    d = make_dir(tmp_path, {"0001-a.md": record()})
    assert run_new(d) == 0
    created = d / "0002-new-thing.md"
    assert created.read_text().startswith("# 0002 New thing\nStatus: accepted 2026-11-20\n")
    assert "0002-new-thing.md" in (d / "README.md").read_text()
    assert decisions.problems(d) == []


def test_new_refuses_a_record_over_the_limit_and_writes_nothing(tmp_path, capsys):
    d = make_dir(tmp_path, {"0001-a.md": record()})
    before = sorted(p.name for p in d.iterdir())
    assert run_new(d, decision=" ".join(["w"] * 120)) == 1
    assert sorted(p.name for p in d.iterdir()) == before
    assert "over" in capsys.readouterr().err


def test_new_supersedes_by_changing_only_the_old_status_line(tmp_path):
    d = make_dir(tmp_path, {"0001-a.md": record()})
    old_before = (d / "0001-a.md").read_text().splitlines()
    assert run_new(d, supersedes="0001") == 0
    old_after = (d / "0001-a.md").read_text().splitlines()
    assert old_after[1] == "Status: superseded by 0002 (accepted 2026-11-12)"
    assert old_after[:1] + old_after[2:] == old_before[:1] + old_before[2:]
    assert decisions.problems(d) == []


def test_new_cannot_supersede_a_missing_record(tmp_path):
    d = make_dir(tmp_path, {"0001-a.md": record()})
    assert run_new(d, supersedes="0007") == 1
    assert not (d / "0002-new-thing.md").exists()


def test_check_exits_nonzero_only_on_a_problem(tmp_path):
    bad = make_dir(tmp_path, {"0001-a.md": record(status="maybe")}, "bad")
    good = make_dir(tmp_path, {"0001-a.md": record()}, "good")
    assert decisions.main(["check", "--dir", str(bad)]) == 1
    assert decisions.main(["check", "--dir", str(good)]) == 0
