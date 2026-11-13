import sys

import pytest

from codecheck.runner import CommandError, SubprocessRunner, run_checked
from fakes import FakeRunner


def test_subprocess_runner_captures_output():
    res = SubprocessRunner().run([sys.executable, "-c", "print('hi')"])
    assert (res.returncode, res.stdout.strip()) == (0, "hi")


def test_subprocess_runner_missing_binary_is_127_not_exception():
    assert SubprocessRunner().run(["definitely-not-a-binary-xyz"]).returncode == 127


def test_subprocess_runner_timeout_is_124():
    res = SubprocessRunner().run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)
    assert res.returncode == 124


def test_run_checked_raises_with_stderr():
    runner = FakeRunner().on("x", returncode=2, stderr="boom")
    with pytest.raises(CommandError, match="boom"):
        run_checked(runner, ["x"])


def test_fake_runner_exact_then_prefix_and_unexpected():
    runner = FakeRunner().on("a", "b", stdout="1").on_prefix("a", stdout="2")
    assert runner.run(["a", "b"]).stdout == "1"
    assert runner.run(["a", "z"]).stdout == "2"
    with pytest.raises(AssertionError):
        runner.run(["nope"])
