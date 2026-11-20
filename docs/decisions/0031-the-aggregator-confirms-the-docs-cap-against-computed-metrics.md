# 0031 The aggregator confirms the docs cap against computed metrics
Status: accepted 2026-11-20
Decision: The aggregator may read docs-metrics.json and confirms a docs comment-cap finding against its over_cap and comment_percent, not by recounting. This is an exception to decision 0020: the gap is structural, not a missed finding.
Why: A per-PR ratio cannot be confirmed from one anchor line, and the aggregator is told to drop what it cannot verify.
Rejected: Waiting for a live snippet to fail twice; adding the file to run_files for every reviewer; letting the aggregator recount.
