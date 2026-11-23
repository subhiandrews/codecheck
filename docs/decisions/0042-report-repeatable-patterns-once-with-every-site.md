# 0042 Report repeatable patterns once with every site
Status: accepted 2026-11-23
Decision: Reviewers grep the change for other instances of a repeatable pattern and report it once, naming every file:line. The aggregator merges the pattern across files. address-comments sweeps the PR and rechecks deferrals against its own fixes.
Why: In hermetic PR 2 all seven findings of rounds 2 and 3 were already present in round 1, reported one file per round.
Rejected: A second sweep pass; raising the suggestion cap.
