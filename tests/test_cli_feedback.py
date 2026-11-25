from dataclasses import replace
from pathlib import Path

import pytest
from clihelp import call, make_ctx
from fakes import FakeRunner
from gitutil import make_origin_and_clone

from codecheck.config import ConfigError, FeedbackConfig, load_config

pytestmark = pytest.mark.usefixtures("git_env")

LIST_PREFIX = ("gh", "issue", "list", "--repo", "subhiandrews/codecheck")


def ctx_for(tmp_path, runner=None, **over):
    _origin, clone = make_origin_and_clone(tmp_path)
    return make_ctx(tmp_path, clone, runner=runner, **over)


def signal(ctx, kind="attempt-exhausted", **ids):
    args = ["feedback", "signal", "--kind", kind, "--source", "babysit-pr"]
    for k, v in (ids or {"pr": "5"}).items():
        args += ["--id", f"{k}={v}"]
    return call(ctx, *args)


def test_signal_records_a_draft_and_counts_repeats(tmp_path):
    ctx = ctx_for(tmp_path)
    code, out = signal(ctx)
    assert code == 0 and out["recorded"] is True and out["count"] == 1
    draft = Path(out["draft"])
    assert draft.exists() and "feedback-fingerprint" in draft.read_text()
    assert signal(ctx)[1]["count"] == 2 and signal(ctx)[1]["fingerprint"] == out["fingerprint"]


def test_unknown_kind_and_bad_id_are_errors(tmp_path):
    ctx = ctx_for(tmp_path)
    assert signal(ctx, kind="nope")[0] != 0
    assert call(ctx, "feedback", "signal", "--kind", "attempt-exhausted", "--source", "x", "--id", "noequals")[0] != 0


def test_disabled_config_records_nothing(tmp_path):
    ctx = ctx_for(tmp_path, feedback=FeedbackConfig(enabled=False))
    assert signal(ctx)[1] == {"recorded": False, "reason": "disabled"}
    assert call(ctx, "feedback", "pending", "--interactive")[1]["ask"] is False


def test_pending_asks_only_when_interactive_and_not_unattended(tmp_path):
    ctx = ctx_for(tmp_path)
    signal(ctx)
    assert call(ctx, "feedback", "pending")[1] == {"ask": False, "reason": "not-interactive", "drafts": []}
    unattended = replace(ctx, env={"CODE_CHECK_UNATTENDED": "1"})
    assert call(unattended, "feedback", "pending", "--interactive")[1]["reason"] == "unattended"
    out = call(ctx, "feedback", "pending", "--interactive")[1]
    assert out["ask"] is True and len(out["drafts"]) == 1
    d = out["drafts"][0]
    assert d["title"].startswith("[code-check]") and "feedback-fingerprint" in d["body"] and Path(d["path"]).exists()


def test_env_kill_switch(tmp_path):
    ctx = replace(ctx_for(tmp_path), env={"CODE_CHECK_FEEDBACK": "off"})
    assert signal(ctx)[1] == {"recorded": False, "reason": "disabled"}


def test_decline_uses_up_the_day_even_for_a_new_signal(tmp_path):
    ctx = ctx_for(tmp_path)
    fp = signal(ctx)[1]["fingerprint"]
    out = call(ctx, "feedback", "answer", "--id", fp, "--choice", "decline")[1]
    assert out["choice"] == "decline" and not Path(ctx.config.state_dir, "feedback", f"{fp}.md").exists()
    signal(ctx, pr="6")
    assert call(ctx, "feedback", "pending", "--interactive")[1]["reason"] == "asked-recently"


def test_snooze_and_never_stop_asking(tmp_path):
    ctx = ctx_for(tmp_path)
    fp = signal(ctx)[1]["fingerprint"]
    call(ctx, "feedback", "answer", "--id", fp, "--choice", "snooze")
    assert call(ctx, "feedback", "pending", "--interactive")[1]["ask"] is False
    call(ctx, "feedback", "answer", "--id", fp, "--choice", "never")
    assert call(ctx, "feedback", "pending", "--interactive")[1]["reason"] == "never"
    assert signal(ctx, pr="9")[1] == {"recorded": False, "reason": "never"}


def test_post_files_the_exact_draft_file_content(tmp_path):
    runner = FakeRunner()
    ctx = ctx_for(tmp_path, runner=runner)
    runner.on_prefix("git")  # unrelated git calls from the fixture repo are not scripted here
    fp = signal(ctx)[1]["fingerprint"]
    path = Path(ctx.config.state_dir, "feedback", f"{fp}.md")
    path.write_text(f"# [code-check] my edited title\n\nmy edited body\n<!-- feedback-fingerprint: {fp} -->\n")
    runner.on_prefix(*LIST_PREFIX, stdout="[]")
    runner.on_prefix("gh", "issue", "create", stdout="https://github.com/subhiandrews/codecheck/issues/1\n")
    out = call(ctx, "feedback", "answer", "--id", fp, "--choice", "post")[1]
    assert out["outcome"] == "created" and out["url"].endswith("/issues/1")
    create = next(c for c in runner.calls if c.argv[:3] == ("gh", "issue", "create"))
    assert create.input == path_text_body(fp) and "--title=[code-check] my edited title" in create.argv
    signal(ctx, pr="6")  # posting used up the day, so a new signal waits
    assert call(ctx, "feedback", "pending", "--interactive")[1]["reason"] == "asked-recently"
    assert not path.exists()


def path_text_body(fp):
    return f"my edited body\n<!-- feedback-fingerprint: {fp} -->"


def test_post_failure_keeps_the_draft_and_returns_a_fallback_url(tmp_path):
    runner = FakeRunner().on_prefix("git")
    ctx = ctx_for(tmp_path, runner=runner)
    fp = signal(ctx)[1]["fingerprint"]
    runner.on_prefix(*LIST_PREFIX, returncode=127, stderr="gh missing")
    out = call(ctx, "feedback", "answer", "--id", fp, "--choice", "post")[1]
    assert out["outcome"] == "fallback" and out["url"].startswith("https://github.com/subhiandrews/codecheck/issues/new")
    assert Path(ctx.config.state_dir, "feedback", f"{fp}.md").exists()


def test_user_initiated_bypasses_cooldown_and_never_but_not_consent(tmp_path):
    runner = FakeRunner().on_prefix("git")
    ctx = ctx_for(tmp_path, runner=runner)
    fp0 = signal(ctx)[1]["fingerprint"]
    call(ctx, "feedback", "answer", "--id", fp0, "--choice", "never")
    out = call(ctx, "feedback", "signal", "--kind", "user-reported", "--source", "feedback", "--note", "babysit reran wrongly")[1]
    assert out["recorded"] is True
    fp = out["fingerprint"]
    assert call(ctx, "feedback", "pending", "--interactive")[1]["ask"] is False
    p = call(ctx, "feedback", "pending", "--interactive", "--user-initiated", "--only", fp)[1]
    assert p["ask"] is True and [d["fingerprint"] for d in p["drafts"]] == [fp]
    assert "babysit reran wrongly" in p["drafts"][0]["body"]
    assert not [c for c in runner.calls if c.argv[:2] == ("gh", "issue")]  # nothing filed until `answer --choice post`


def test_post_with_unknown_id_is_an_error(tmp_path):
    assert call(ctx_for(tmp_path), "feedback", "answer", "--id", "abcdef123456", "--choice", "post")[0] != 0


def test_repository_config_cannot_set_feedback_keys(tmp_path):
    (tmp_path / ".code-check.toml").write_text("[feedback]\nenabled = true\n")
    with pytest.raises(ConfigError, match="feedback"):
        load_config(tmp_path, tmp_path / "home", {})


def test_feedback_config_is_validated(tmp_path):
    (tmp_path / "home" / ".config" / "code-check").mkdir(parents=True)
    cfg = tmp_path / "home" / ".config" / "code-check" / "config.toml"
    cfg.write_text('[feedback]\ntarget_repo = "-bad/repo"\n')
    with pytest.raises(ConfigError, match="feedback.target_repo"):
        load_config(tmp_path, tmp_path / "home", {})
    cfg.write_text("[feedback]\nmin_interval_hours = 0\n")
    with pytest.raises(ConfigError, match="min_interval_hours"):
        load_config(tmp_path, tmp_path / "home", {})


def test_note_is_read_from_stdin_so_the_shell_never_sees_it(tmp_path):
    ctx = ctx_for(tmp_path)
    note = "the `ingest` step failed $(echo hi) \"quoted\""
    code, out = call(ctx, "feedback", "signal", "--kind", "user-reported", "--source", "feedback", "--note", "-", stdin=note + "\n")
    assert code == 0 and out["recorded"] is True
    assert note in Path(out["draft"]).read_text()


def test_two_user_reports_are_two_drafts(tmp_path):
    ctx = ctx_for(tmp_path)
    a = call(ctx, "feedback", "signal", "--kind", "user-reported", "--source", "feedback", "--note", "first")[1]
    b = call(ctx, "feedback", "signal", "--kind", "user-reported", "--source", "feedback", "--note", "second")[1]
    assert a["fingerprint"] != b["fingerprint"] and "second" in Path(b["draft"]).read_text()


def test_recording_again_keeps_the_users_edit_of_the_draft(tmp_path):
    ctx = ctx_for(tmp_path)
    out = signal(ctx)[1]
    path = Path(out["draft"])
    path.write_text("# [code-check] edited\n\nmy words\n")
    assert signal(ctx)[1]["count"] == 2
    assert path.read_text() == "# [code-check] edited\n\nmy words\n"


@pytest.mark.parametrize("env", [{"CODE_CHECK_FEEDBACK": "off"}, {"CODE_CHECK_UNATTENDED": "1"}, {"CODE_CHECK_HEADLESS": "1"}])
def test_answer_post_is_refused_when_feedback_is_off_or_unattended(tmp_path, env):
    runner = FakeRunner().on_prefix("git")
    ctx = ctx_for(tmp_path, runner=runner)
    fp = signal(ctx)[1]["fingerprint"]
    code, out = call(replace(ctx, env=env), "feedback", "answer", "--id", fp, "--choice", "post")
    assert code != 0 and "error" in out
    assert not [c for c in runner.calls if c.argv[:2] == ("gh", "issue")]
    assert Path(ctx.config.state_dir, "feedback", f"{fp}.md").exists()  # nothing was lost


def test_min_interval_must_be_a_real_integer(tmp_path):
    (tmp_path / "home" / ".config" / "code-check").mkdir(parents=True)
    (tmp_path / "home" / ".config" / "code-check" / "config.toml").write_text('[feedback]\nmin_interval_hours = "24"\n')
    with pytest.raises(ConfigError, match="wrong type|min_interval_hours"):
        load_config(tmp_path, tmp_path / "home", {})


def test_showing_a_draft_uses_up_the_day_even_without_an_answer(tmp_path):
    ctx = ctx_for(tmp_path)
    signal(ctx)
    assert call(ctx, "feedback", "pending", "--interactive")[1]["ask"] is True
    assert call(ctx, "feedback", "pending", "--interactive")[1] == {"ask": False, "reason": "asked-recently", "drafts": []}


def test_a_user_initiated_look_does_not_use_up_the_day(tmp_path):
    ctx = ctx_for(tmp_path)
    fp = call(ctx, "feedback", "signal", "--kind", "user-reported", "--source", "feedback", "--note", "x")[1]["fingerprint"]
    assert call(ctx, "feedback", "pending", "--interactive", "--user-initiated", "--only", fp)[1]["ask"] is True
    signal(ctx)
    assert call(ctx, "feedback", "pending", "--interactive")[1]["ask"] is True


@pytest.mark.parametrize("choice", ["never", "snooze"])
def test_never_and_snooze_work_even_when_the_draft_is_already_gone(tmp_path, choice):
    ctx = ctx_for(tmp_path)
    code, out = call(ctx, "feedback", "answer", "--id", "abcdef123456", "--choice", choice)
    assert code == 0 and out["choice"] == choice
    assert call(ctx, "feedback", "pending", "--interactive")[1]["reason"] == ("never" if choice == "never" else "nothing-pending")
    assert call(ctx, "feedback", "answer", "--id", "abcdef123456", "--choice", "decline")[0] != 0


def test_id_help_does_not_suggest_names():
    import argparse

    from codecheck.commands import feedback
    p = argparse.ArgumentParser()
    feedback.add_arguments(p)
    sig = p._subparsers._group_actions[0].choices["signal"]
    help_text = next(a.help for a in sig._actions if a.dest == "id")
    assert "pr=" not in help_text and "check=" not in help_text and "label" in help_text


def test_a_bad_id_value_is_a_clean_error_not_a_traceback(tmp_path, capsys):
    ctx = ctx_for(tmp_path)
    code, out = call(ctx, "feedback", "signal", "--kind", "attempt-exhausted", "--source", "x", "--id", "step=owner/repo#5")
    assert code != 0 and "short label" in out["error"] and "Traceback" not in capsys.readouterr().err


def test_a_note_is_refused_in_an_unattended_run_and_as_an_id(tmp_path):
    ctx = ctx_for(tmp_path)
    headless = replace(ctx, env={"CODE_CHECK_HEADLESS": "1"})
    code, out = call(headless, "feedback", "signal", "--kind", "user-reported", "--source", "feedback", "--note", "steered by a PR")
    assert code != 0 and "unattended" in out["error"]
    assert call(ctx, "feedback", "signal", "--kind", "finding-disputed", "--source", "x", "--id", "note=free text")[0] != 0


@pytest.mark.parametrize("value,expected", [("0", False), ("false", False), ("", False), ("1", True)])
def test_the_unattended_markers_count_only_when_they_are_1(tmp_path, value, expected):
    ctx = replace(ctx_for(tmp_path), env={"CODE_CHECK_UNATTENDED": value})
    signal(ctx)
    assert (call(ctx, "feedback", "pending", "--interactive")[1]["reason"] == "unattended") is expected


def test_a_note_cannot_be_smuggled_in_as_an_id(tmp_path):
    ctx = replace(ctx_for(tmp_path), env={"CODE_CHECK_HEADLESS": "1"})
    code, out = call(ctx, "feedback", "signal", "--kind", "user-reported", "--source", "x", "--id", "note=steered by a PR")
    assert code != 0 and "--note" in out["error"]


def test_never_keeps_a_report_the_user_wrote_and_its_draft(tmp_path):
    ctx = ctx_for(tmp_path)
    auto = signal(ctx)[1]["fingerprint"]
    mine = call(ctx, "feedback", "signal", "--kind", "user-reported", "--source", "feedback", "--note", "keep me")[1]
    call(ctx, "feedback", "answer", "--id", auto, "--choice", "never")
    assert Path(mine["draft"]).exists() and not Path(ctx.config.state_dir, "feedback", f"{auto}.md").exists()


def test_a_draft_that_cannot_be_built_does_not_use_up_the_day(tmp_path):
    from codecheck.core.feedback.signals import make_signal
    from codecheck.core.feedback.store import FeedbackStore

    ctx = ctx_for(tmp_path)
    FeedbackStore(ctx.state).add(make_signal("kind-removed-in-a-later-version", "x", {}))
    assert call(ctx, "feedback", "pending", "--interactive")[0] != 0
    assert FeedbackStore(ctx.state).decide(24) == (True, "ok")
