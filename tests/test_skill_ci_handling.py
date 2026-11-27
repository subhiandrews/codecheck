"""#106: a failing CI check must never be silently skipped by the babysit and address-comments skills.

These are text checks on the skills: the behaviour lives in the model's reading of them, so what the skills say is the
only thing a test can pin. The CLI side (`checks`, `attempt`, `check-log`, the providers) has its own tests."""

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _step(skill: str, number: int) -> str:
    text = (REPO / "skills" / skill / "SKILL.md").read_text()
    prefix = f"{number}. "
    return next(line for line in text.splitlines() if line.startswith(prefix))


def test_address_comments_reaches_the_ci_step_when_it_has_nothing_to_address():
    nothing = _step("address-comments", 3)
    assert "code-check checks --pr <PR>" in nothing and "skip to step 12" in nothing and "nothing to address" in nothing


def test_address_comments_final_report_carries_the_ci_outcome():
    report = _step("address-comments", 13)
    assert "CI:" in report and "unrelated or flaky" in report and "need a human" in report


def test_address_comments_reruns_a_check_it_judges_unrelated_or_flaky_and_reports_it():
    """#111: a check judged unrelated stayed red for ever, so a merge rule that needs green CI stuck the PR."""
    text = (REPO / "skills" / "address-comments" / "SKILL.md").read_text()
    step = next(line for line in text.splitlines() if line.lstrip().startswith("c. Decide whether THIS PR"))
    assert "code-check rerun-check --pr <PR> --check <name>" in step and "once" in step
    assert "(<r> rerun)" in _step("address-comments", 13)


def test_babysit_delegates_comments_and_ci_to_address_comments():
    step = _step("babysit-pr", 1)
    assert "address-comments" in step and "failing CI" in step
