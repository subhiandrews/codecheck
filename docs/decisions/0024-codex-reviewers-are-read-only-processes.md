# 0024 Codex reviewers are read-only processes
Status: accepted 2026-11-19
Decision: On Codex the CLI starts each reviewer as its own read-only `codex exec` process and writes the findings files itself.
Why: Subagents inherit the parent's sandbox, which cannot be read-only. Standalone read-only processes denied all 11 writes.
Rejected: Codex subagents with `sandbox_mode = "read-only"`, which had no effect.
Evidence: docs/spikes/2026-11-19-codex.md
