# 0011 Skip PRs already approved at the current head
Status: accepted 2026-11-13
Decision: `precheck` skips a PR that someone approved at its current head. Approvals on older commits do not count. `--force` overrides.
Why: An approved PR needs no further review tokens.
Rejected: None recorded.
