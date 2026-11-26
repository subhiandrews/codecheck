# 0057 hand-off comments get related findings by shared words
Status: accepted 2026-11-26
Decision: Each unmatched human comment in handoff.json carries related: up to 3 kept or dropped findings in the same file sharing 3 distinctive words. A pointer, not a match: matched_finding, misses and unraised_findings are unchanged.
Why: Location matching found 3 of 25 human comments on the 2026-11-26 runs; about 5 more named the same concern from other lines, some against findings the aggregator dropped.
Rejected: A model judge per pair: tokens on every hand-off. Replacing the location match: changes recorded recall. Cross-file matching: too many false pairs.
