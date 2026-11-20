# 0007 Hand-offs are read from the session transcript
Status: accepted 2026-11-13
Decision: With `shadow.handoff` on, a final skill step copies and scrubs the Claude Code session transcript and extracts signals into a hand-off folder.
Why: It needs no hooks and sees what agents do between CLI calls.
Rejected: Live tool-call hooks (unreliable run linkage, a hook in every session) and CLI-only records (blind between calls).
