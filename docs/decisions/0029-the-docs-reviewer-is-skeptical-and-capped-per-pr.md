# 0029 The docs reviewer is skeptical and capped per PR
Status: accepted 2026-11-19
Decision: A docs reviewer treats comments and documentation as costs. Code measures added comment lines against added code lines, capped at 3% per PR, and only an explicit CODE_CHECK.md rule overrides it.
Why: A whole-codebase ratio punishes a PR for old debt and needs a full scan.
Rejected: A whole-codebase ratio; asking for more documentation; a cap the PR itself can change.
