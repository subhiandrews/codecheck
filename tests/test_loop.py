import json
import os
import signal
import subprocess
import sys
import time
from argparse import Namespace
from dataclasses import replace
from pathlib import Path

import pytest
from clihelp import call, make_ctx
from fakes import FakeProvider, FakeRunner, HybridRunner
from gitutil import make_origin_and_clone

from codecheck.commands import loop
from codecheck.config import LoopConfig
from codecheck.core.state import FileLock
from codecheck.models import Check
from codecheck.runner import CommandResult

pytestmark = pytest.mark.usefixtures("git_env")

OK_JSON = json.dumps({"type": "result", "is_error": False, "result": "o/r#1: COMMENT, 2 inline comments",
                      "total_cost_usd": 0.12, "usage": {"input_tokens": 10, "output_tokens": 5}})


def setup(tmp_path, n_prs=1, claude=None):
    origin, clone = make_origin_and_clone(tmp_path)
    provider = FakeProvider()
    provider.me = "me"
    provider.clone_argv = lambda repo, dest: ["git", "clone", "-q", str(origin), dest]
    refs = [provider.add_pr(n, head_sha=f"sha{n}").ref for n in range(1, n_prs + 1)]
    provider.listing["review-requested"] = refs
    fake = claude or FakeRunner().on_prefix("claude", stdout=OK_JSON)
    ctx = make_ctx(tmp_path, clone, provider=provider, runner=HybridRunner(fake))
    return ctx, provider, fake, refs, clone


def claude_calls(fake):
    return [c for c in fake.calls if c.argv[0] == "claude"]


def test_dry_run_shows_the_headless_command_and_marks_nothing(tmp_path):
    ctx, _provider, fake, refs, _ = setup(tmp_path)
    _code, out = call(ctx, "run-pr", "--mode", "review", "--pr", str(refs[0]), "--dry-run")
    argv = out["argv"]
    assert argv[:2] == ["claude", "-p"] and "o/r#1" in argv[2] and "review-pr" in argv[2]
    assert "--output-format" in argv and "json" in argv and "--permission-mode" in argv and "dontAsk" in argv
    assert claude_calls(fake) == [] and ctx.state.get(f"review:{refs[0]}") is None
    assert ctx.state.get(f"loop:review:{refs[0]}") is None


def test_run_pr_success_parses_result_runs_in_repo_clone_and_marks_processed(tmp_path):
    ctx, _provider, fake, refs, clone = setup(tmp_path)
    _code, out = call(ctx, "run-pr", "--mode", "review", "--pr", str(refs[0]), "--fingerprint", "sha1")
    assert out["ok"] is True and "2 inline comments" in out["summary"] and out["cost_usd"] == 0.12
    (c,) = claude_calls(fake)
    assert c.cwd == clone  # the child starts inside a clone of the PR's repo
    assert c.input == ""
    assert ctx.state.get(f"loop:review:{refs[0]}") == "sha1"
    assert ctx.state.get(f"review:{refs[0]}") is None  # only `post` records "this head was reviewed"
    usage = [r for rid in ctx.shadow.run_ids() for r in ctx.shadow.read(rid) if r["type"] == "headless_run"]
    assert usage and usage[0]["pr"] == "o/r#1"


@pytest.mark.parametrize("stdout,returncode,needle", [
    ("", 1, "exit"),
    ("not json at all", 0, "not json"),
    (json.dumps({"is_error": True, "result": "max turns reached"}), 0, "max turns"),
    ("", 124, "timed out"),
])
def test_failures_are_reported_retried_and_never_mark_the_head_reviewed(tmp_path, stdout, returncode, needle):
    fake = FakeRunner().on_prefix("claude", stdout=stdout, returncode=returncode, stderr="timed out after 1800s" if returncode == 124 else "boom")
    ctx, _provider, _, refs, _ = setup(tmp_path, claude=fake)
    code, out = call(ctx, "run-pr", "--mode", "review", "--pr", str(refs[0]), "--fingerprint", "sha1")
    assert code == 0 and out["ok"] is False and needle in out["summary"].lower()
    assert out["failures"] == 1 and out["gave_up"] is False
    assert ctx.state.get(f"review:{refs[0]}") is None  # a failed run never counts as "already reviewed"
    assert ctx.state.get(f"loop:review:{refs[0]}") is None  # so the next iteration retries it
    code, pre = call(ctx, "precheck", "--mode", "pr", "--pr", str(refs[0]))
    assert "already reviewed" not in pre.get("reason", "")


def test_a_pr_that_keeps_failing_is_given_up_after_three_attempts_until_it_changes(tmp_path):
    fake = FakeRunner().on_prefix("claude", stdout="", returncode=1, stderr="usage limit reached")
    ctx, provider, _, refs, _ = setup(tmp_path, claude=fake)
    outs = [call(ctx, "loop", "--mode", "review")[1] for _ in range(4)]
    assert [o["processed"] for o in outs] == [1, 1, 1, 0]
    assert [o["results"][0]["failures"] for o in outs[:3]] == [1, 2, 3]
    assert outs[2]["results"][0]["gave_up"] is True
    assert ctx.state.get(f"review:{refs[0]}") is None
    provider.prs[refs[0]] = replace(provider.prs[refs[0]], head_sha="sha-new")
    out = call(ctx, "loop", "--mode", "review")[1]
    assert out["processed"] == 1 and out["results"][0]["failures"] == 1  # the count restarts for a new head


def test_success_after_a_failure_clears_the_failure_count(tmp_path):
    fake = FakeRunner().on_prefix("claude", stdout="", returncode=1, stderr="boom")
    fake._prefix[0][1].append(CommandResult(0, OK_JSON, ""))  # first call fails, later calls succeed
    ctx, _provider, _, refs, _ = setup(tmp_path, claude=fake)
    call(ctx, "loop", "--mode", "review")
    assert ctx.state.get(f"loop:review:{refs[0]}:failures")["count"] == 1
    out = call(ctx, "loop", "--mode", "review")[1]
    assert out["results"][0]["ok"] is True
    assert ctx.state.get(f"loop:review:{refs[0]}") == "sha1"
    assert ctx.state.get(f"loop:review:{refs[0]}:failures") is None


def test_review_loop_skips_a_head_already_reviewed_by_hand(tmp_path):
    ctx, _provider, _fake, refs, _ = setup(tmp_path, n_prs=2)
    ctx.state.set(f"review:{refs[0]}", "sha1")
    out = call(ctx, "loop", "--mode", "review")[1]
    assert [r["pr"] for r in out["results"]] == [str(refs[1])]


def test_one_pr_that_errors_while_listing_does_not_abort_the_iteration(tmp_path):
    ctx, provider, _fake, refs, _ = setup(tmp_path, n_prs=3)
    real_get = provider.get_pr

    def flaky(ref):
        if ref == refs[1]:
            raise RuntimeError("gh: HTTP 502")
        return real_get(ref)

    provider.get_pr = flaky
    out = call(ctx, "loop", "--mode", "review")[1]
    assert [r["pr"] for r in out["results"]] == [str(refs[0]), str(refs[2])]
    assert out["skipped"] == [{"pr": str(refs[1]), "reason": "error: RuntimeError: gh: HTTP 502"}]


def test_loop_processes_at_most_five_prs_one_at_a_time_then_the_rest(tmp_path):
    ctx, _provider, fake, refs, _ = setup(tmp_path, n_prs=7)
    _code, out = call(ctx, "loop", "--mode", "review")
    assert out["processed"] == 5 and [r["pr"] for r in out["results"]] == [str(r) for r in refs[:5]]
    assert len(claude_calls(fake)) == 5
    _code, out = call(ctx, "loop", "--mode", "review")
    assert out["processed"] == 2 and [r["pr"] for r in out["results"]] == [str(r) for r in refs[5:]]


def test_idle_loop_spends_nothing(tmp_path):
    ctx, _provider, fake, _refs, _ = setup(tmp_path, n_prs=2)
    call(ctx, "loop", "--mode", "review")
    before = len(claude_calls(fake))
    _code, out = call(ctx, "loop", "--mode", "review")
    assert out["processed"] == 0 and len(claude_calls(fake)) == before


def test_new_push_makes_a_pr_reappear(tmp_path):
    ctx, provider, _fake, refs, _ = setup(tmp_path)
    call(ctx, "loop", "--mode", "review")
    provider.prs[refs[0]] = replace(provider.prs[refs[0]], head_sha="newsha")
    _code, out = call(ctx, "loop", "--mode", "review")
    assert out["processed"] == 1


def test_overlapping_iteration_is_skipped_while_the_lock_is_held(tmp_path):
    ctx, _provider, fake, _refs, _ = setup(tmp_path)
    lock = FileLock(tmp_path / "state" / "locks" / "review.lock")
    assert lock.acquire()
    _code, out = call(ctx, "loop", "--mode", "review")
    assert out["skipped"] is True and "still running" in out["reason"] and claude_calls(fake) == []
    lock.release()
    assert call(ctx, "loop", "--mode", "review")[1]["processed"] == 1


def test_babysit_loop_uses_babysit_prompt_and_failing_checks(tmp_path):
    ctx, provider, fake, _refs, clone = setup(tmp_path)
    pr = provider.add_pr(9, author="me", head_sha="mine1")
    provider.checks[pr.ref] = [Check("build", "fail")]
    provider.listing["authored"] = [pr.ref]
    _code, out = call(ctx, "loop", "--mode", "babysit")
    assert out["processed"] == 1
    (c,) = claude_calls(fake)
    assert "babysit-pr" in c.argv[2] and "o/r#9" in c.argv[2]
    # babysit edits code, so the unattended run never starts in the user's own checkout
    assert c.cwd != clone and c.cwd == tmp_path / "state" / "repos" / "o__r"


def test_unattended_babysit_never_uses_the_users_checkout_even_on_the_pr_branch(tmp_path):
    ctx, provider, fake, _refs, clone = setup(tmp_path)
    pr = provider.add_pr(9, author="me", head_sha="mine1", head_ref="main")  # user is sitting on the PR branch
    call(ctx, "run-pr", "--mode", "babysit", "--pr", str(pr.ref))
    (c,) = claude_calls(fake)
    assert c.cwd != clone and Path(c.cwd).is_relative_to(tmp_path / "state")


def test_failed_clone_leaves_nothing_behind_and_has_a_timeout(tmp_path):
    ctx, provider, _fake, refs, _clone = setup(tmp_path)
    provider.clone_argv = lambda repo, dest: ["git", "clone", "-q", str(tmp_path / "missing"), dest]
    seen = []
    real = ctx.runner.real.run

    def spy(args, **kw):
        seen.append((args[:2], kw.get("timeout")))
        return real(args, **kw)

    ctx.runner.real.run = spy
    out = call(ctx, "run-pr", "--mode", "babysit", "--pr", str(refs[0]))[1]
    assert out["ok"] is False and "could not clone" in out["summary"]
    assert (["git", "clone"], 900) in [(list(a), t) for a, t in seen]
    assert not (tmp_path / "state" / "repos" / "o__r").exists()


def test_loop_config_cap_is_respected(tmp_path):
    ctx, _provider, _fake, _refs, _ = setup(tmp_path, n_prs=4)
    ctx.config = replace(ctx.config, loop=LoopConfig(max_prs_per_iteration=2))
    assert call(ctx, "loop", "--mode", "review")[1]["processed"] == 2


def _lock_path(tmp_path):
    return tmp_path / "state" / "locks" / "review.lock"


def test_lock_is_refreshed_before_each_pr_and_names_this_process(tmp_path):
    ctx, _provider, fake, _refs, _ = setup(tmp_path, n_prs=2)
    seen = []
    real = fake.run

    def spy(args, **kw):
        lock = _lock_path(tmp_path)
        seen.append((lock.read_text().split(":")[0], lock.stat().st_mtime))
        os.utime(lock, (1, 1))  # pretend this PR took forever
        return real(args, **kw)

    fake.run = spy
    call(ctx, "loop", "--mode", "review")
    assert [pid for pid, _ in seen] == [str(os.getpid())] * 2
    assert seen[1][1] > time.time() - 60  # touched again before the second PR
    assert not _lock_path(tmp_path).exists()


def test_stale_threshold_covers_a_full_iteration_of_slow_prs(tmp_path):
    ctx, *_ = setup(tmp_path)
    assert loop.stale_after(ctx.config) >= 5 * (1800 + 900)  # 5 PRs at the run timeout plus the clone timeout
    ctx.config = replace(ctx.config, loop=LoopConfig(max_prs_per_iteration=1))
    assert loop.stale_after(ctx.config) < 5 * 1800


def test_a_live_iteration_older_than_two_hours_is_not_reclaimed(tmp_path):
    ctx, _provider, fake, _refs, _ = setup(tmp_path)
    lock = _lock_path(tmp_path)
    lock.parent.mkdir(parents=True)
    lock.write_text(f"{os.getpid()}:someone-else")  # a live process holds it
    old = time.time() - 3 * 3600
    os.utime(lock, (old, old))
    out = call(ctx, "loop", "--mode", "review")[1]
    assert out["skipped"] is True and claude_calls(fake) == []


def test_a_lock_whose_holder_died_is_reclaimed_at_once(tmp_path):
    ctx, _provider, _fake, _refs, _ = setup(tmp_path)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    lock = _lock_path(tmp_path)
    lock.parent.mkdir(parents=True)
    lock.write_text(f"{dead.pid}:killed-by-a-bash-timeout")
    out = call(ctx, "loop", "--mode", "review")[1]
    assert out["processed"] == 1


def test_sigterm_mid_iteration_releases_the_lock_and_restores_handlers(tmp_path):
    ctx, _provider, fake, _refs, _ = setup(tmp_path, n_prs=2)
    before = signal.getsignal(signal.SIGTERM)

    def killed(args, **kw):
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(1)  # the handler fires here
        raise AssertionError("SIGTERM handler did not run")

    fake.run = killed
    with pytest.raises(SystemExit) as exc:
        loop.run(Namespace(mode="review", repos="", dry_run=False), ctx)
    assert exc.value.code == 128 + signal.SIGTERM
    assert not _lock_path(tmp_path).exists()
    assert signal.getsignal(signal.SIGTERM) is before


def test_headless_argv_scopes_tools_per_mode_and_adds_the_state_dir(tmp_path):
    ctx, _provider, _fake, refs, _ = setup(tmp_path)
    review = call(ctx, "run-pr", "--mode", "review", "--pr", str(refs[0]), "--dry-run")[1]["argv"]
    babysit = call(ctx, "run-pr", "--mode", "babysit", "--pr", str(refs[0]), "--dry-run")[1]["argv"]
    state = str((tmp_path / "state").resolve())
    for argv in (review, babysit):
        assert argv[argv.index("--add-dir") + 1] == state
        assert argv.index("--add-dir") < argv.index("--allowedTools")  # variadic flags come after the prompt
        assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert "Edit" not in review[review.index("--allowedTools"):]
    assert "Edit" in babysit[babysit.index("--allowedTools"):]
    ctx.config = replace(ctx.config, run_pr=replace(ctx.config.run_pr, add_state_dir=False))
    assert "--add-dir" not in call(ctx, "run-pr", "--mode", "review", "--pr", str(refs[0]), "--dry-run")[1]["argv"]


def test_run_pr_refuses_tool_entries_that_would_act_as_options(tmp_path):
    ctx, _provider, fake, refs, _ = setup(tmp_path)
    ctx.config = replace(ctx.config, run_pr=replace(ctx.config.run_pr, review_allowed_tools=("Read", "--verbose")))
    code, out = call(ctx, "run-pr", "--mode", "review", "--pr", str(refs[0]))
    assert code == 1 and "must not start with '-'" in out["error"] and claude_calls(fake) == []


def test_review_loop_skips_fork_prs_unless_enabled(tmp_path):
    ctx, provider, _fake, refs, _ = setup(tmp_path, n_prs=2)
    provider.prs[refs[0]] = replace(provider.prs[refs[0]], is_cross_repo=True)
    out = call(ctx, "loop", "--mode", "review")[1]
    assert [r["pr"] for r in out["results"]] == [str(refs[1])]
    assert out["skipped"][0]["pr"] == str(refs[0]) and "review_forks" in out["skipped"][0]["reason"]
    ctx.config = replace(ctx.config, loop=replace(ctx.config.loop, review_forks=True))
    out = call(ctx, "loop", "--mode", "review")[1]
    assert [r["pr"] for r in out["results"]] == [str(refs[0])]


def test_run_pr_security_flag_extends_the_review_prompt(tmp_path):
    ctx, _provider, _fake, refs, _ = setup(tmp_path)
    base = ("Use the code-check review-pr skill on o/r#1. Run unattended: never ask questions; "
            "finish with one line summarizing the outcome.")
    plain = call(ctx, "run-pr", "--mode", "review", "--pr", str(refs[0]), "--dry-run")[1]["argv"]
    sec = call(ctx, "run-pr", "--mode", "review", "--pr", str(refs[0]), "--security", "--dry-run")[1]["argv"]
    assert plain[2] == base
    assert sec[2] == base + " Pass the security argument."


def test_run_pr_security_flag_is_review_only(tmp_path):
    ctx, _provider, fake, refs, _ = setup(tmp_path)
    code, out = call(ctx, "run-pr", "--mode", "babysit", "--pr", str(refs[0]), "--security", "--dry-run")
    assert code != 0 and "review mode only" in json.dumps(out)
    assert claude_calls(fake) == []


def _reply_setup(tmp_path):
    from codecheck.models import Review, Thread, ThreadComment

    ctx, provider, fake, refs, _ = setup(tmp_path, n_prs=2)
    provider.reviews[refs[0]] = [Review("me", "sha1", "2026-01-01T00:00:00Z", "COMMENTED")]
    provider.threads[refs[0]] = [Thread("T1", "a.py", 1, False, False, (
        ThreadComment(1, "me", "nit", "2026-01-01T00:00:01Z"),
        ThreadComment(2, "alice", "why?", "2026-01-02T00:00:00Z")))]
    ctx.state.set(f"review:{refs[0]}", "sha1")
    return ctx, provider, fake, refs


def test_loop_surfaces_replies_only_pr_without_running_it(tmp_path):
    ctx, _provider, fake, refs = _reply_setup(tmp_path)
    out = call(ctx, "loop", "--mode", "review")[1]
    assert [r["pr"] for r in out["results"]] == [str(refs[1])]
    assert len(claude_calls(fake)) == 1
    (skip,) = [s for s in out["skipped"] if s["pr"] == str(refs[0])]
    assert "1 reply on 1 of your threads" in skip["reason"]
    assert out["considered"] == 2


def test_replies_only_pr_stays_outside_the_cap(tmp_path):
    from dataclasses import replace

    ctx, _provider, _fake, refs = _reply_setup(tmp_path)
    ctx.config = replace(ctx.config, loop=LoopConfig(max_prs_per_iteration=1))
    out = call(ctx, "loop", "--mode", "review")[1]
    assert [r["pr"] for r in out["results"]] == [str(refs[1])]
    assert str(refs[0]) in [s["pr"] for s in out["skipped"]]


def test_replies_only_pr_runs_again_after_a_new_push(tmp_path):
    from dataclasses import replace

    ctx, provider, _fake, refs = _reply_setup(tmp_path)
    provider.prs[refs[0]] = replace(provider.prs[refs[0]], head_sha="sha1b")
    out = call(ctx, "loop", "--mode", "review")[1]
    assert str(refs[0]) in [r["pr"] for r in out["results"]]
