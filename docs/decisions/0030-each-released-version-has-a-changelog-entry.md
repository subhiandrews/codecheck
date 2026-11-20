# 0030 Each released version has a changelog entry
Status: accepted 2026-11-20
Decision: CHANGELOG.md gets one short entry per version, written in the PR that bumps it. A test ties the top heading to VERSION and CODE_CHECK.md makes a bump without an entry a Warning.
Why: A version bump says that something changed but not what; release notes written later get skipped or invented.
Rejected: Generating notes from git log; backfilling old releases; one changelog per component.
