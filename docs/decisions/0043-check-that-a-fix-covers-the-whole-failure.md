# 0043 Check that a fix covers the whole failure
Status: accepted 2026-11-23
Decision: Reviewers re-reading an [ours] finding whose code changed check that the fix covers the whole failure and report the gap as a new finding. address-comments re-reads the changed function for the same failure class and makes a guard or cap cover the whole.
Why: In hermetic PR 6 two round-2 findings were incomplete fixes of round 1: encode left outside the try, and a per-item cap that left the total unbounded.
Rejected: Leaving it to the next review round.
