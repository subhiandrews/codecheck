# 0014 Repository config cannot set security-sensitive keys
Status: accepted 2026-11-13
Decision: A repository's `.code-check.toml` may not set `state_dir`, `run_pr`, `shadow`, `golden`, `codex`, `review.auto_approve`, `review.attribution`, `review.label`, `review.login`, `loop.review_forks`, `address.test_command` or `deep_reviewer.command`. Such a key is an error.
Why: A reviewed repository is untrusted.
Rejected: Treating it like user config.
