# 0018 Reviewers see every existing comment on the PR
Status: accepted 2026-11-13
Decision: In PR mode `context` writes all review threads to `existing-comments.md` for reviewers and the aggregator, except in blind hand-off runs. A fetch failure does not stop the review.
Why: Reviews repeated, contradicted or ignored what humans had already written.
Rejected: A second file for human reviews (two untrusted blobs to reconcile); aggregator-only access.
