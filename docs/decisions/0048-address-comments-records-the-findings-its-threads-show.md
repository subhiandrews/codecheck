# 0048 Address-comments records the findings its threads show
Status: accepted 2026-11-24
Decision: threads records the code-check findings it sees in a PR's threads (ours, or a bot's with our marker) as finding and outcome records, so verdicts on CI reviews become guidance evidence for your own repositories.
Why: CI reviews write to a store this machine never sees, so own-repo evidence was empty. The thread holds the finding text and the author's verdict follows.
Rejected: Recording local review findings; recording human comments as misses, which are false when CI did not review.
