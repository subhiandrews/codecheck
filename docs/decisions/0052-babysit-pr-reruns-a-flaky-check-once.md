# 0052 babysit-pr reruns a flaky check once
Status: accepted 2026-11-25
Decision: When babysit-pr judges a failing check unrelated to the PR, it reruns that check once with rerun-check and reports it, instead of leaving the PR red.
Why: A merge rule that needs green CI left hermetic#38 stuck on a Mongo exit 137 flake until a person reran the job (#111). The rerun counts against the same three attempts per check.
Rejected: Retrying a refused rerun in a loop: the host answered HTTP 500 once, and a loop would burn attempts. Rerunning checks the PR caused: that hides the defect.
