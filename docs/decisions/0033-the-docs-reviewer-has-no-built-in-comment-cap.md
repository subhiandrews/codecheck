# 0033 The docs reviewer has no built-in comment cap
Status: accepted 2026-11-21
Decision: Remove the 3% comment cap, max_comment_percent and docs_min_comment_lines. Docs routes only when a documentation file changes. A repo that wants a comment limit writes it in its own CODE_CHECK.md; the reviewer measures it with docs-metrics.json counts. Old config keys are ignored.
Why: The cap raised a warning on repos that never asked: 5 of 13 kept findings in the 2026-11-20 runs, none of those repos had a CODE_CHECK.md.
Rejected: A lower or higher default cap; keeping the cap behind a config key; removing docs-metrics.json
