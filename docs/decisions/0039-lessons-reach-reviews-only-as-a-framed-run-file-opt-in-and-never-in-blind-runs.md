# 0039 Lessons reach reviews only as a framed run file, opt-in and never in blind runs
Status: accepted 2026-11-21
Decision: With memory.enabled and memory.read, lessons arrive as memory.md in run_files: miss lessons for reviewers from context, finding lessons for the aggregator from ingest once no reviewer is missing. Only active or contested lessons whose path still exists are shown, scrubbed and capped. Blind runs and live evals get none.
Why: Memory is unconfirmed, so it stays advisory data and a failure must never break a review.
Rejected: Editing agent prompts (decision 0020); injecting candidates; showing lessons to blind runs.
