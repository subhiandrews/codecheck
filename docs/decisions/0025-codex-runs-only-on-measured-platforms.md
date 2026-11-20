# 0025 Codex runs only on measured platforms
Status: accepted 2026-11-19
Decision: `panel run` refuses on an unmeasured platform unless the user sets `codex.allow_unmeasured_platform`, a user-only key.
Why: Every sandbox measurement was taken on macOS.
Rejected: Treating other platforms as equivalent.
Evidence: docs/spikes/2026-11-19-codex.md
