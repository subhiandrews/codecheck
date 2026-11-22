# 0039 review-pr without an argument reviews the repository's open PRs
Status: accepted 2026-11-22
Decision: With no argument the review loop lists the open PRs of the repository it runs in (or loop.repos), newest first, reviews at most 3 per iteration, and stops listing once the cap is met. Reviewers also get CLAUDE.md, read from the base branch like CODE_CHECK.md.
Why: Review-requested needs someone to ask; running in a repo is the ask. Newest first serves fresh work; the head check stops re-reviews.
Rejected: Keeping the review-requested queue; one shared cap of 5; CLAUDE.md from the PR head.
Evidence: src/codecheck/commands/list_work.py
