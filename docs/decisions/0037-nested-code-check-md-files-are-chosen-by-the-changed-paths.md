# 0037 Nested CODE_CHECK.md files are chosen by the changed paths
Status: accepted 2026-11-22
Decision: A CODE_CHECK.md in any directory above a changed file is read with the root one and applies only to files under that directory. All share the 16,000 character cap, with room kept for nested files. PR runs read the base branch.
Why: Monorepo applications need their own rules. Changed paths give PR, local and unattended runs the same answer.
Rejected: Choosing by the starting directory, which PR and unattended runs lack; a cap per file; changing agent prompts.
Evidence: src/codecheck/core/guidance.py
