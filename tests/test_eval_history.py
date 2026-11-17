import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from clihelp import call, make_ctx
from gitutil import sh
from test_eval_judge import PROBE, ScriptedJudge, _case, _worth, _write_cases

from codecheck.config import Config
from codecheck.evals import history
from codecheck.evals.history import (
    RECORD,
    REPORT,
    load,
    record,
    regenerate,
    render,
    summarize,
)
from codecheck.runner import SubprocessRunner

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def docs(tmp_path, git_env):
    """A real git repository with a docs/ directory, the way the plugin repository is laid out."""
    repo = tmp_path / "plugin"
    repo.mkdir()
    sh(repo, "init", "-b", "main")
    (repo / "docs").mkdir()
    (repo / "docs" / "keep").write_text("x")
    sh(repo, "add", ".")
    sh(repo, "commit", "-m", "init")
    return repo / "docs"


def _ctx():
    return SimpleNamespace(runner=SubprocessRunner(), config=Config())


LIVE = {"passed": False, "cases": [
    {"name": "a", "result": {"passed": True}, "claude": {"cost_usd": 0.5}},
    {"name": "b", "result": {"passed": False}, "claude": {"cost_usd": None}},
    {"name": "c", "result": {"passed": True}, "claude": {"cost_usd": 0.25}}]}
CAL = {"passed": False, "agreement": {"n": 14, "agree": 7, "rate": 0.5, "kappa": -0.32, "errors": 0, "extra": 1},
       "cost_usd": 1.0, "cases": []}


def _entry(mode, ts, **over):
    base = {"ts": ts, "sha": "a" * 40, "branch": "main", "dirty": False, "mode": mode, "partial": None, "passed": True,
            "models": {"reviewers": "sonnet", "aggregator": "opus", "judge": "opus"}, "cost_usd": 1.0}
    if mode == "live":
        base["cases"] = {"n": 4, "passed": 3, "failed": ["x"]}
    else:
        base["agreement"] = {"n": 10, "agree": 8, "rate": 0.8, "kappa": 0.4, "errors": 0}
    return {**base, **over}


# --- what a run is reduced to ------------------------------------------------------------------------------------


def test_live_summary_counts_cases_and_sums_the_known_costs():
    assert summarize("live", LIVE) == {"passed": False, "cases": {"n": 3, "passed": 2, "failed": ["b"]}, "cost_usd": 0.75}


def test_calibration_summary_keeps_only_the_agreement_numbers():
    s = summarize("calibrate", CAL)
    assert s["agreement"] == {"n": 14, "agree": 7, "rate": 0.5, "kappa": -0.32, "errors": 0} and s["cost_usd"] == 1.0


@pytest.mark.parametrize("mode, result", [
    ("live", {"passed": True, "skipped": "model not reachable: x", "cases": []}),
    ("live", {"passed": True, "cases": []}),
    ("calibrate", {"passed": False, "agreement": {"n": 0}, "error": "no labelled findings to calibrate against"}),
])
def test_a_run_that_measured_nothing_is_not_summarized(mode, result):
    assert summarize(mode, result) is None


# --- the record --------------------------------------------------------------------------------------------------


def test_recording_appends_a_line_and_rebuilds_the_report(docs):
    path = record(_ctx(), "live", LIVE, None, docs)
    record(_ctx(), "calibrate", CAL, None, docs)
    assert path == docs / RECORD
    lines = [json.loads(ln) for ln in path.read_text().splitlines()]
    assert [e["mode"] for e in lines] == ["live", "calibrate"]
    first = lines[0]
    assert first["sha"] == sh(docs.parent, "rev-parse", "HEAD").strip() and first["branch"] == "main"
    assert first["dirty"] is False and lines[1]["dirty"] is True  # the first line itself made the tree dirty
    assert first["partial"] is None and first["models"]["reviewers"] == "sonnet" and "judge" not in first["models"]
    assert lines[1]["models"] == {"judge": "opus"}
    assert (docs / REPORT).read_text().count("```mermaid") == 2


def test_nothing_is_recorded_without_a_docs_directory_or_a_measurement(tmp_path, docs):
    assert record(_ctx(), "live", LIVE, None, tmp_path / "missing") is None
    assert record(_ctx(), "live", {"passed": True, "skipped": "x", "cases": []}, None, docs) is None
    assert not (docs / RECORD).exists()


def test_load_sorts_by_time_and_counts_unusable_lines(tmp_path):
    f = tmp_path / RECORD
    f.write_text("\n".join([json.dumps(_entry("live", "2026-11-03T00:00:00+00:00")), "{not json", '"a string"',
                            json.dumps({"mode": "other", "ts": "2026-11-01T00:00:00+00:00"}),
                            json.dumps(_entry("live", "2026-11-01T00:00:00+00:00"))]) + "\n")
    entries, bad = load(f)
    assert [e["ts"][:10] for e in entries] == ["2026-11-01", "2026-11-03"] and bad == 3
    assert load(tmp_path / "missing.jsonl") == ([], 0)


@pytest.mark.parametrize("bad_line", [
    {"mode": "live", "ts": "2026-11-01T00:00:00+00:00"},                                         # no cases
    {"mode": "live", "ts": "2026-11-01T00:00:00+00:00", "cases": []},                            # cases is a list
    {"mode": "live", "ts": "2026-11-01T00:00:00+00:00", "cases": {"n": 4}},                      # no passed
    {"mode": "live", "ts": "2026-11-01T00:00:00+00:00", "cases": {"n": "4", "passed": 3}},       # not numbers
    {"mode": "live", "ts": "2026-11-01T00:00:00+00:00", "cases": {"n": 4, "passed": 3, "failed": [1]}},
    {"mode": "calibrate", "ts": "2026-11-01T00:00:00+00:00", "agreement": {"n": 4}},             # no agree
    {"mode": "calibrate", "ts": "2026-11-01T00:00:00+00:00", "agreement": {"n": 4, "agree": 2, "kappa": "x"}},
    {"mode": "calibrate", "ts": "2026-11-01T00:00:00+00:00", "agreement": {"n": 4, "agree": 2}, "cost_usd": "1"},
])
def test_a_line_missing_a_field_the_report_reads_is_unusable_and_cannot_break_it(tmp_path, bad_line):
    f = tmp_path / RECORD
    f.write_text(json.dumps(_entry("live", "2026-11-02T00:00:00+00:00")) + "\n" + json.dumps(bad_line) + "\n")
    entries, bad = load(f)
    assert bad == 1 and len(entries) == 1
    assert "1 unusable line(s)" in render(entries, bad) and regenerate(tmp_path)["unusable_lines"] == 1


# --- the report --------------------------------------------------------------------------------------------------


def test_report_charts_each_mode_with_a_rolling_average():
    entries = [_entry("live", f"2026-11-0{i}T00:00:00+00:00", cases={"n": 4, "passed": p, "failed": []})
               for i, p in ((1, 4), (2, 2), (3, 3))]
    text = render(entries)
    assert "x-axis [1, 2, 3]" in text and "line [100, 50, 75]" in text
    assert "line [100, 75, 75]" in text  # trailing average of up to three runs
    assert "No runs recorded yet." in text  # nothing for the judge


def test_report_table_lists_the_newest_run_first_with_its_context():
    entries = [_entry("live", "2026-11-01T00:00:00+00:00", sha="1" * 40),
               _entry("live", "2026-11-02T00:00:00+00:00", sha="2" * 40, dirty=True, branch="feat|x",
                      cases={"n": 4, "passed": 3, "failed": ["tiny-bug"]})]
    rows = [ln for ln in render(entries).splitlines() if ln.startswith("| ") and ln[2].isdigit()]
    assert rows[0].startswith("| 2 | 2026-11-02 | 2222222+dirty | feat\\|x | 75% | 3/4 (failed: tiny-bug) | reviewers sonnet")
    assert rows[1].startswith("| 1 | 2026-11-01 | 1111111 |")


def test_partial_runs_stay_in_the_record_but_out_of_the_report():
    entries = [_entry("live", "2026-11-01T00:00:00+00:00"), _entry("live", "2026-11-02T00:00:00+00:00", partial="tiny-bug")]
    text = render(entries)
    assert "x-axis [1]" in text and "2026-11-02" not in text


def test_the_chart_and_table_keep_the_run_numbers_when_cut_to_the_latest_runs():
    total = history.CHART_RUNS + 5
    entries = [_entry("live", f"2026-10-{i:02d}T00:00:00+00:00") for i in range(1, total + 1)]
    text = render(entries)
    assert f"x-axis [{total - history.CHART_RUNS + 1}, " in text and f"{total}]" in text
    rows = [ln for ln in text.splitlines() if ln.startswith("| ") and ln[2].isdigit()]
    assert len(rows) == history.TABLE_RUNS and rows[0].startswith(f"| {total} |")


def test_history_command_rebuilds_the_report_and_says_what_changed(docs):
    (docs / RECORD).write_text("\n".join(json.dumps(_entry("live", f"2026-11-0{i}T00:00:00+00:00",
                                                           cases={"n": 4, "passed": p, "failed": []}))
                                         for i, p in ((1, 2), (2, 3))) + "\n")
    out = regenerate(docs)
    assert out["runs"] == {"live": 2, "calibrate": 0} and out["live"] == {"latest": 75.0, "previous": 50.0}
    assert "calibrate" not in out and (docs / REPORT).exists()
    assert regenerate(docs / "missing") == {"passed": True, "runs": {"live": 0, "calibrate": 0}, "report": None}


# --- through the CLI ---------------------------------------------------------------------------------------------


class GitAwareRunner(ScriptedJudge):
    """Answers git for real (the history records the commit) and everything else as the scripted judge."""

    def run(self, args, **kw):
        if list(args)[:1] == ["git"]:
            return SubprocessRunner().run(args, **kw)
        return super().run(args, **kw)


@pytest.fixture
def cli(tmp_path, docs, monkeypatch):
    from codecheck.commands import eval_run

    cases = _write_cases(tmp_path / "c", _case("c1", ["expected", "not_expected"]))
    monkeypatch.setattr(history, "DEFAULT_DIR", docs)
    monkeypatch.setattr(eval_run, "DEFAULT_CASES", cases)
    runner = GitAwareRunner(_worth(lambda p: "finding 0" in p))
    return make_ctx(tmp_path, tmp_path, runner=runner), docs


def test_a_full_calibration_run_is_recorded_and_its_output_is_unchanged(cli, capsys):
    ctx, docs = cli
    code, out = call(ctx, "eval", "--calibrate")
    assert code == 0 and out["passed"] is True
    assert set(out) == {"agreement", "controls", "skipped", "cost_usd", "unpriced_calls", "cases", "passed"}
    entries, _ = load(docs / RECORD)
    assert [e["mode"] for e in entries] == ["calibrate"] and entries[0]["agreement"]["n"] == 2
    assert "commit both" in capsys.readouterr().err


def test_no_record_partial_and_custom_runs_stay_out_of_the_trend(cli, tmp_path):
    ctx, docs = cli
    call(ctx, "eval", "--calibrate", "--no-record")
    call(ctx, "eval", "--calibrate", "--cases", str(tmp_path / "c"))
    assert not (docs / RECORD).exists()
    call(ctx, "eval", "--calibrate", "--only", "c1")
    entries, _ = load(docs / RECORD)
    assert [e["partial"] for e in entries] == ["c1"]
    assert "x-axis" not in (docs / REPORT).read_text()


def test_a_failure_to_record_never_changes_the_eval(cli, capsys):
    ctx, docs = cli
    (docs / RECORD).mkdir()  # appending to a directory fails
    code, out = call(ctx, "eval", "--calibrate")
    assert code == 0 and out["passed"] is True and out["agreement"]["n"] == 2
    assert ctx.runner.calls[0][0] == PROBE
    assert "warning: eval history not recorded" in capsys.readouterr().err


def test_replay_refuses_no_record_and_history_takes_no_other_options(cli):
    ctx, _ = cli
    code, out = call(ctx, "eval", "--replay", "--no-record")
    assert code == 1 and "--live and --calibrate only" in out["error"]
    code, out = call(ctx, "eval", "--history", "--only", "x")
    assert code == 1 and "--history takes no other options" in out["error"]
    code, out = call(ctx, "eval", "--history")
    assert code == 0 and out["passed"] is True and out["runs"] == {"live": 0, "calibrate": 0}


# --- the repository's own files ----------------------------------------------------------------------------------


def test_the_record_merges_by_union_and_the_report_is_marked_generated():
    attrs = (REPO / ".gitattributes").read_text()
    assert "docs/eval-history.jsonl merge=union" in attrs and "docs/eval-history.md linguist-generated=true" in attrs


def test_the_committed_report_matches_the_committed_record():
    record_file = REPO / "docs" / RECORD
    if not record_file.exists():
        pytest.skip("no history recorded yet")
    entries, bad = load(record_file)
    assert bad == 0 and (REPO / "docs" / REPORT).read_text() == render(entries)
