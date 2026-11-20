# 0019 The aggregator keeps the highest verified severity
Status: accepted 2026-11-13
Decision: When reviewers rate one root cause differently, keep the highest severity the aggregator can verify and explain any downgrade. Findings that are only out-of-scope or missing docs are capped at suggestion.
Why: Missing a real bug costs more than noise, and scope remarks are noise.
Rejected: Silent downgrades.
