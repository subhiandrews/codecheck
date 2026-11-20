# 0016 CODE_CHECK.md is read from the base branch
Status: accepted 2026-11-13
Decision: PR runs read `CODE_CHECK.md` from the base branch, local runs from the working tree. Longer than 16,000 characters is truncated.
Why: A PR author cannot change the rules their own PR is reviewed under.
Rejected: Reading it from the PR head.
