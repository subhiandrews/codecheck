# 0038 Guidance learning is opt-in, local and never commits
Status: accepted 2026-11-22
Decision: review-local may write a checked CODE_CHECK.md proposal to the working tree when review.learn_guidance is on. Evidence is the local shadow store; a pattern needs two PRs. The CLI limits size, changed lines and secrets; the model only drafts. Nothing is staged or committed.
Why: A rule that reaches reviews changes every later one, so a person commits it. A PR author's verdicts are one side's view.
Rejected: Auto-commit or a standing PR, which needs a token and a workflow; a model-written lesson store (0036); CI evidence now.
Evidence: src/codecheck/core/learn.py
