import json
from dataclasses import replace

import pytest

from clihelp import call, make_ctx
from codecheck.config import LoopConfig
from codecheck.core.state import FileLock
from codecheck.models import Check
from fakes import FakeProvider, FakeRunner, HybridRunner
from gitutil import make_origin_and_clone

pytestmark = pytest.mark.usefixtures("git_env")

OK_JSON = json.dumps({"type": "result", "is_error": False, "result": "o/r#1: COMMENT, 2 inline comments",
                      "total_cost_usd": 0.12, "usage": {"input_tokens": 10, "output_tokens": 5}})


def setup(tmp_path, n_prs=1, claude=None):
    _, clone = make_origin_and_clone(tmp_path)
    provider = FakeProvider()
    provider.me = "me"
    refs = [provider.add_pr(n, head_sha=f"sha{n}").ref for n in range(1, n_prs + 1)]
    provider.listing["review-requested"] = refs
    fake = claude or FakeRunner().on_prefix("claude", stdout=OK_JSON)
    ctx = make_ctx(tmp_path, clone, provider=provider, runner=HybridRunner(fake))
    return ctx, provider, fake, refs, clone


def claude_calls(fake):
    return [c for c in fake.calls if c.argv[0] == "claude"]


def test_dry_run_shows_the_headless_command_and_marks_nothing(tmp_path):
    ctx, provider, fake, refs, _ = setup(tmp_path)
    code, out = call(ctx, "run-pr", "--mode", "review", "--pr", str(refs[0]), "--dry-run")
    argv = out["argv"]
    assert argv[:2] == ["claude", "-p"] and "o/r#1" in argv[2] and "review-pr" in argv[2]
    assert "--output-format" in argv and "json" in argv and "--permission-mode" in argv and "dontAsk" in argv
    assert claude_calls(fake) == [] and ctx.state.get(f"review:{refs[0]}") is None


def test_run_pr_success_parses_result_runs_in_repo_clone_and_marks_processed(tmp_path):
    ctx, provider, fake, refs, clone = setup(tmp_path)
    code, out = call(ctx, "run-pr", "--mode", "review", "--pr", str(refs[0]), "--fingerprint", "sha1")
    assert out["ok"] is True and "2 inline comments" in out["summary"] and out["cost_usd"] == 0.12
    (c,) = claude_calls(fake)
    assert c.cwd == clone  # the child starts inside a clone of the PR's repo
    assert c.input == ""
    assert ctx.state.get(f"review:{refs[0]}") == "sha1"
    usage = [r for rid in ctx.shadow.run_ids() for r in ctx.shadow.read(rid) if r["type"] == "headless_run"]
    assert usage and usage[0]["pr"] == "o/r#1"


@pytest.mark.parametrize("stdout,returncode,needle", [
    ("", 1, "exit"),
    ("not json at all", 0, "not json"),
    (json.dumps({"is_error": True, "result": "max turns reached"}), 0, "max turns"),
    ("", 124, "timed out"),
])
def test_failures_are_reported_and_still_marked_processed(tmp_path, stdout, returncode, needle):
    fake = FakeRunner().on_prefix("claude", stdout=stdout, returncode=returncode, stderr="timed out after 1800s" if returncode == 124 else "boom")
    ctx, provider, _, refs, _ = setup(tmp_path, claude=fake)
    code, out = call(ctx, "run-pr", "--mode", "review", "--pr", str(refs[0]), "--fingerprint", "sha1")
    assert code == 0 and out["ok"] is False and needle in out["summary"].lower()
    assert ctx.state.get(f"review:{refs[0]}") == "sha1"  # not retried every tick


def test_loop_processes_at_most_five_prs_one_at_a_time_then_the_rest(tmp_path):
    ctx, provider, fake, refs, _ = setup(tmp_path, n_prs=7)
    code, out = call(ctx, "loop", "--mode", "review")
    assert out["processed"] == 5 and [r["pr"] for r in out["results"]] == [str(r) for r in refs[:5]]
    assert len(claude_calls(fake)) == 5
    code, out = call(ctx, "loop", "--mode", "review")
    assert out["processed"] == 2 and [r["pr"] for r in out["results"]] == [str(r) for r in refs[5:]]


def test_idle_loop_spends_nothing(tmp_path):
    ctx, provider, fake, refs, _ = setup(tmp_path, n_prs=2)
    call(ctx, "loop", "--mode", "review")
    before = len(claude_calls(fake))
    code, out = call(ctx, "loop", "--mode", "review")
    assert out["processed"] == 0 and len(claude_calls(fake)) == before


def test_new_push_makes_a_pr_reappear(tmp_path):
    ctx, provider, fake, refs, _ = setup(tmp_path)
    call(ctx, "loop", "--mode", "review")
    provider.prs[refs[0]] = replace(provider.prs[refs[0]], head_sha="newsha")
    code, out = call(ctx, "loop", "--mode", "review")
    assert out["processed"] == 1


def test_overlapping_iteration_is_skipped_while_the_lock_is_held(tmp_path):
    ctx, provider, fake, refs, _ = setup(tmp_path)
    lock = FileLock(tmp_path / "state" / "locks" / "review.lock")
    assert lock.acquire()
    code, out = call(ctx, "loop", "--mode", "review")
    assert out["skipped"] is True and "still running" in out["reason"] and claude_calls(fake) == []
    lock.release()
    assert call(ctx, "loop", "--mode", "review")[1]["processed"] == 1


def test_babysit_loop_uses_babysit_prompt_and_failing_checks(tmp_path):
    ctx, provider, fake, refs, _ = setup(tmp_path)
    pr = provider.add_pr(9, author="me", head_sha="mine1")
    provider.checks[pr.ref] = [Check("build", "fail")]
    provider.listing["authored"] = [pr.ref]
    code, out = call(ctx, "loop", "--mode", "babysit")
    assert out["processed"] == 1
    (c,) = claude_calls(fake)
    assert "babysit-pr" in c.argv[2] and "o/r#9" in c.argv[2]


def test_loop_config_cap_is_respected(tmp_path):
    ctx, provider, fake, refs, _ = setup(tmp_path, n_prs=4)
    ctx.config = replace(ctx.config, loop=LoopConfig(max_prs_per_iteration=2))
    assert call(ctx, "loop", "--mode", "review")[1]["processed"] == 2
