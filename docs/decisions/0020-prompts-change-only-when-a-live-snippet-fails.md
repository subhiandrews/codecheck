# 0020 Prompts change only when a live snippet fails
Status: accepted 2026-11-13
Decision: Change a reviewer prompt only after a live snippet that exposes the gap fails twice.
Why: A failing test shows what to fix, and a passing one shows nothing is needed.
Rejected: A dedicated concurrency reviewer: cost and noise on every run for one miss.
