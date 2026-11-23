# 0041 review-pr proposes guidance as a patch for a separate PR
Status: accepted 2026-11-22
Decision: With review.learn_guidance on, review-pr may write a checked CODE_CHECK.md patch against the PR's base ref to the state directory. It never touches the PR, a working tree or the remote; the report suggests a separate PR.
Why: A teammate's PR and your clone must stay untouched, and a learned rule needs a person to commit it (0038). cleanup removes the run directory, so the patch lives beside it.
Rejected: Pushing to the PR; writing into a clone; a standing PR needing a token.
