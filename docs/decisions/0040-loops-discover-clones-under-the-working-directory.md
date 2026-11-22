# 0040 Loops discover clones under the working directory
Status: accepted 2026-11-22
Decision: review and babysit loops work on the clone they run in, or every clone directly under the directory, named by its remote. Unattended babysit works in a worktree of that clone, never its checkout (CODE_CHECK_UNATTENDED). review-prs and babysit-prs are aliases.
Why: Where you run it says which repos you mean; no queue, no second clone.
Rejected: A global author search; a managed clone per repo; deeper scans.
Evidence: src/codecheck/vcs/discover.py
