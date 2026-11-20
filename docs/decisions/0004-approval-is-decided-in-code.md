# 0004 Approval is decided in code
Status: accepted 2026-11-12
Decision: Approve only when `review.auto_approve` is on and no Critical or Warning remains. Never approve a draft.
Why: The decision follows from findings and configuration, so a prompt cannot change it.
Rejected: Approval logic in prompts.
