# 0022 Unposted reviews are still compared
Status: accepted 2026-11-13
Decision: When a PR review was not posted, `handoff` still compares its findings with the PR's current threads and marks `comparison_mode: unposted`.
Why: Stale and dry runs still show what humans said.
Rejected: Skipping the comparison.
