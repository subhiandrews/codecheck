"""#106: a failing CI check must never be silently skipped by the babysit and address-comments skills.

These are text checks on the skills: the behaviour lives in the model's reading of them, so what the skills say is the
only thing a test can pin. The CLI side (`checks`, `attempt`, `check-log`, the providers) has its own tests."""

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _step(skill: str, number: int) -> str:
    text = (REPO / "skills" / skill / "SKILL.md").read_text()
    prefix = f"{number}. "
    return next(line for line in text.splitlines() if line.startswith(prefix))


def test_babysit_goes_on_to_the_ci_step_when_address_comments_has_nothing_to_address():
    step = _step("babysit-pr", 2)
    assert "nothing to address" in step
    assert "does not end this run" in step and "step 3" in step


def test_address_comments_names_failing_checks_even_when_it_has_nothing_to_address():
    nothing = _step("address-comments", 3)
    assert "failing" in nothing and "code-check checks --pr <PR>" in nothing


def test_address_comments_final_report_carries_the_failing_checks():
    report = _step("address-comments", 12)
    assert "code-check checks --pr <PR>" in report
    assert "CI:" in report and "failing" in report and "babysit-pr" in report


def test_babysit_reruns_a_check_it_judges_unrelated_or_flaky_and_reports_it():
    """#111: a check judged unrelated stayed red for ever, so a merge rule that needs green CI stuck the PR."""
    text = (REPO / "skills" / "babysit-pr" / "SKILL.md").read_text()
    step = next(line for line in text.splitlines() if line.lstrip().startswith("c. "))
    assert "code-check rerun-check --pr <PR> --check <name>" in step and "once" in step
    report = _step("babysit-pr", 4)
    assert "rerun" in report
