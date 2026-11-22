# 0036 A bot keeps the CI code-check pin current
Status: accepted 2026-11-22
Decision: CI pins code-check-ref to a release tag. A scheduled workflow opens a bot PR when a newer tag exists on main. It never merges, never downgrades and never rewrites a SHA pin.
Why: Dependabot cannot update a free-text action input, and an unpinned input fell back to v0.1.5 for months. A pin that moves only through a reviewed PR keeps CI reviews reproducible.
Rejected: Using latest or main: unreviewed code reviews every PR. Auto-merging the bot PR: it edits CI config, so a human approves it.
