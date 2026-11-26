# 0056 the babysit loop counts rounds per PR and stops at five
Status: accepted 2026-11-26
Decision: list-work counts each unattended run that moved a PR's head, per PR, and skips the PR at loop.max_babysit_rounds (5). After a round it also skips a PR with under 3 open threads, all Suggestions, and no failing check. address-comments declines multi-line Suggestion fixes after a round.
Why: Hermetic PRs needed up to 6 rounds; every round-2 finding was a Suggestion. A headless run cannot count rounds; state.json can.
Rejected: Skill-text counter: each run starts empty. Counting pushes: one run can push twice. Limiting named runs.
