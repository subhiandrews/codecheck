# 0017 Only the user can turn off the credit line or label
Status: accepted 2026-11-13
Decision: Each posted review ends with one credit line and the PR gets a `code-check` label. `review.attribution` and `review.label` are user-config only.
Why: A repository must not suppress the credit or make the tool apply arbitrary labels.
Rejected: Rendering the credit in the provider; labelling from a skill step a model could skip.
