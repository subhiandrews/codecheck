# 0008 Hand-off runs are blind to existing comments
Status: accepted 2026-11-13
Decision: In a hand-off run, PR mode withholds existing comments and replies from reviewers and reports `blind: true`.
Why: Findings can then be compared with what humans said independently. Blind runs may repeat a human's point, so they are for evaluation.
Rejected: Showing comments in hand-off runs, which would contaminate the comparison.
